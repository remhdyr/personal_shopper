"""The primary hosted dashboard.

A tiny dependency-free HTTP server (stdlib only) that shows every deal which
cleared the dashboard bar, with images and the same feedback buttons as
Telegram. It's the main way to browse deals: reach it over your private VPN
(e.g. Tailscale) whenever you like. Telegram is a *push* on top of this for the
exceptional, freshly-posted hits; the dashboard is the always-there feed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import ValidationError

from .config import WatchItem
from .db import Database
from .logging_setup import get_logger
from .logistics import Logistics
from .models import VERDICT_DESCRIPTIONS, VERDICT_LABELS, DealAnalysis, Feedback, Listing, Verdict

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .triggers import ManualTrigger

log = get_logger(__name__)

try:
    _GIT_HASH: str = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True
    ).strip()
except Exception:  # noqa: BLE001 - non-critical, fall back gracefully
    _GIT_HASH = "unknown"


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.0f} {currency}".replace(",", " ")


def _card(listing: Listing, analysis: DealAnalysis | None, logistics: Logistics,
          verdict: Verdict | None = None, *, backfill: bool = False) -> dict:
    price = (
        _money(listing.price, listing.currency) if listing.price is not None else "price unknown"
    )
    value = (
        _money(analysis.estimated_value, listing.currency)
        if analysis is not None and analysis.estimated_value is not None
        else "n/a"
    )
    delivery = "unknown"
    if analysis is not None:
        cost = logistics.estimate(listing, analysis)
        if cost.total is not None:
            delivery = f"~{_money(cost.total, listing.currency)} total via {cost.method}"
        elif cost.pickup_cost is not None:
            delivery = f"pickup ~{_money(cost.pickup_cost, listing.currency)}"
    return {
        "uid": listing.uid,
        "source": listing.source.value,
        "watch_item": listing.watch_item,
        "title": listing.title,
        "url": listing.url,
        "image_url": listing.image_urls[0] if listing.image_urls else None,
        "price": price,
        "value": value,
        "deal_score": analysis.deal_score if analysis is not None else None,
        "fit_score": analysis.fit_score if analysis is not None else None,
        "condition": (analysis.condition if analysis is not None else "") or "?",
        "summary": (analysis.summary if analysis is not None else "") or listing.description[:280],
        "model": analysis.model if analysis is not None else "",
        "delivery": delivery,
        "ended": listing.is_ended(),
        "verdict": verdict.value if verdict is not None else None,
        "verdict_label": VERDICT_LABELS[verdict] if verdict is not None else None,
        "backfill": backfill,
        "buttons": [{"verdict": v.value, "label": label} for v, label in VERDICT_LABELS.items()],
    }


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Shopper - local dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; padding: 1rem; }
  h1 { font-size: 1.1rem; color: #999; font-weight: normal; }
  #empty { color: #666; padding: 2rem 0; }
  .card { background: #1c1c1c; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; display: flex; gap: 1rem; }
  .card img { width: 120px; height: 120px; object-fit: cover; border-radius: 8px; flex-shrink: 0; }
  .card .body { flex: 1; min-width: 0; }
  .card a { color: #6cf; text-decoration: none; }
  .source-badge { display: inline-block; margin-left: 0.5rem; padding: 0.1rem 0.45rem; border-radius: 999px; background: #2b3a4a; color: #9cf; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em; vertical-align: middle; }
  .model-badge { display: inline-block; margin-left: 0.5rem; padding: 0.1rem 0.45rem; border-radius: 999px; background: #3a2b4a; color: #d9b3ff; font-size: 0.7rem; vertical-align: middle; }
  .ended-badge { display: inline-block; margin-left: 0.5rem; padding: 0.1rem 0.45rem; border-radius: 999px; background: #4a2b2b; color: #f99; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em; vertical-align: middle; }
  .card.ended { opacity: 0.55; }
  .card.ended img { filter: grayscale(1); }
  .backfill-badge { display: inline-block; margin-left: 0.5rem; padding: 0.1rem 0.45rem; border-radius: 999px; background: #3a361c; color: #e6d98a; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em; vertical-align: middle; }
  .card.backfill { border: 1px dashed #4a4636; }
  .scores { color: #9c9; font-size: 0.9rem; margin: 0.25rem 0; }
  .delivery, .price { color: #ccc; font-size: 0.9rem; }
  .summary { color: #aaa; font-size: 0.9rem; margin: 0.5rem 0; }
  .buttons { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
  button { background: #333; color: #eee; border: 1px solid #444; border-radius: 6px; padding: 0.4rem 0.7rem; cursor: pointer; font-size: 0.85rem; }
  button:hover { background: #444; }
  button:disabled { opacity: 0.5; cursor: default; }
  .ack { color: #6c6; font-size: 0.85rem; }
  .toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1rem; }
  .toolbar > label { color: #999; font-size: 0.85rem; }
  #source-filters { display: flex; flex-wrap: wrap; gap: 0.6rem; }
  #source-filters label { display: inline-flex; align-items: center; gap: 0.3rem; color: #ccc; font-size: 0.85rem; background: #262626; border: 1px solid #444; border-radius: 6px; padding: 0.25rem 0.5rem; cursor: pointer; }
  #item-filter { background: #262626; color: #eee; border: 1px solid #444; border-radius: 6px; padding: 0.3rem 0.5rem; font: inherit; font-size: 0.85rem; }
  #mute-wrap { display: inline-flex; align-items: center; gap: 0.3rem; color: #ccc; font-size: 0.85rem; cursor: pointer; }
  #send-more-status { color: #999; font-size: 0.85rem; }
  details.panel { background: #1c1c1c; border-radius: 10px; margin-bottom: 1.5rem; padding: 0.5rem 1rem; }
  details.panel > summary { cursor: pointer; color: #9cf; font-size: 0.95rem; padding: 0.4rem 0; }
  .legend { list-style: none; margin: 0.4rem 0 0.2rem; padding: 0; display: grid; gap: 0.4rem; }
  .legend li { display: flex; gap: 0.6rem; align-items: baseline; color: #ccc; font-size: 0.85rem; }
  .legend .legend-label { flex: 0 0 12rem; font-weight: 600; color: #eee; }
  .legend .legend-desc { color: #aaa; }
  .watch-item { border-top: 1px solid #333; padding: 0.6rem 0; }
  .watch-item:first-of-type { border-top: none; }
  .watch-name { font-weight: 600; color: #eee; }
  .watch-prio { margin-left: 0.4rem; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em; color: #fc9; }
  .watch-meta { color: #999; font-size: 0.85rem; margin: 0.2rem 0; }
  .watch-actions { display: flex; gap: 0.5rem; margin-top: 0.3rem; }
  .watch-form { display: grid; gap: 0.5rem; margin-top: 0.5rem; }
  .watch-form label { color: #999; font-size: 0.8rem; display: grid; gap: 0.2rem; }
  .watch-form input, .watch-form textarea { background: #262626; color: #eee; border: 1px solid #444; border-radius: 6px; padding: 0.4rem; font: inherit; }
  .watch-form textarea { resize: vertical; min-height: 2.5rem; }
  .watch-form .row { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  #watch-status, #draft-status { color: #999; font-size: 0.85rem; }
  .danger { border-color: #633; color: #f99; }
  .verdict-badge { display: inline-block; margin-left: 0.5rem; padding: 0.1rem 0.45rem; border-radius: 999px; background: #333; color: #ccc; font-size: 0.7rem; vertical-align: middle; }
  button.dismiss { margin-left: auto; border-color: #444; color: #999; }
  #browse-btn { background: #262626; }
</style>
</head>
<body>
<h1 id="feed-title">Pending deals</h1>
<details class="panel" id="watchlist-panel">
  <summary>Watchlist</summary>
  <div id="watch-items"></div>
  <div class="watch-form">
    <label>Describe what to watch for (the AI drafts an item)
      <textarea id="draft-text" placeholder="e.g. a small metal lathe in good condition under 5000 kr"></textarea>
    </label>
    <div class="row">
      <button id="draft-btn" type="button">\u2728 Draft with AI</button>
      <button id="add-blank-btn" type="button">Add manually</button>
      <span id="draft-status"></span>
    </div>
  </div>
  <div id="watch-editor"></div>
</details>
<details class="panel" id="legend-panel">
  <summary>What do the feedback buttons mean?</summary>
  <ul id="legend-list" class="legend"></ul>
</details>
<div class="toolbar">
  <button id="send-more">\U0001f504 Send more</button>
  <label>Sources</label>
  <div id="source-filters"></div>
  <label for="item-filter">Item</label>
  <select id="item-filter"><option value="">All items</option></select>
  <button id="browse-btn" type="button" disabled>Browse all active</button>
  <label id="mute-wrap" for="mute-toggle" hidden><input type="checkbox" id="mute-toggle"> Mute Telegram</label>
  <span id="send-more-status"></span>
</div>
<div id="empty">Nothing pending right now.</div>
<div id="cards"></div>
<div style="margin-top:1rem;"><button id="clear-all" class="danger" type="button">Clear all</button></div>
<script>
const SOURCE_LABELS = { ebay: "eBay", tradera: "Tradera", auctionet: "Auctionet", klaravik: "Klaravik", blinto: "Blinto", psauction: "PS Auction", blocket: "Blocket", fleasy: "Fleasy", aterbygg: "Återbygg" };

// When set to a watch-item name, the feed shows ALL still-active offers for
// that item (rated or not) instead of the pending queue.
let browseItem = null;

async function refresh() {
  const res = await fetch("/api/pending");
  const cards = await res.json();
  const root = document.getElementById("cards");
  const openUids = new Set(Array.from(root.querySelectorAll("[data-uid]")).map(el => el.dataset.uid));
  const newUids = new Set(cards.map(c => c.uid));

  // Remove cards that are no longer pending.
  for (const el of Array.from(root.children)) {
    if (!newUids.has(el.dataset.uid)) el.remove();
  }

  for (const c of cards) {
    if (openUids.has(c.uid)) continue;
    root.appendChild(makeCard(c, false));
  }
  applyFilter();
}

// Load every still-open offer for one watch item (browse mode).
async function loadBrowse(item) {
  browseItem = item;
  const root = document.getElementById("cards");
  const empty = document.getElementById("empty");
  let cards = [];
  try {
    const res = await fetch("/api/active?item=" + encodeURIComponent(item));
    cards = res.ok ? await res.json() : [];
  } catch (e) {
    cards = [];
  }
  root.textContent = "";
  for (const c of cards) root.appendChild(makeCard(c, true));
  empty.textContent = "No active offers for this item.";
  updateModeUI();
  applyFilter();
}

function exitBrowse() {
  browseItem = null;
  document.getElementById("cards").textContent = "";
  document.getElementById("empty").textContent = "Nothing pending right now.";
  updateModeUI();
  refresh();
}

function updateModeUI() {
  const browseBtn = document.getElementById("browse-btn");
  const item = document.getElementById("item-filter").value;
  const title = document.getElementById("feed-title");
  if (browseItem) {
    browseBtn.textContent = "\u2190 Back to pending";
    browseBtn.disabled = false;
    title.textContent = "Active offers \u2014 " + browseItem;
  } else {
    browseBtn.textContent = "Browse all active";
    browseBtn.disabled = item === "";
    title.textContent = "Pending deals";
  }
}

// Build one deal card. In pending mode a rating removes the card and a Dismiss
// button hides it without a verdict; in browse mode ratings stay put and just
// update the shown verdict badge.
function makeCard(c, browse) {
  const div = document.createElement("div");
  div.className = "card";
  div.dataset.uid = c.uid;
  div.dataset.source = c.source;
  div.dataset.watchItem = c.watch_item || "";

  // Build the card with textContent/setAttribute rather than innerHTML:
  // titles, summaries and URLs come from untrusted marketplace/AI data, so
  // interpolating them into HTML would be an injection vector.
  if (c.image_url && isHttp(c.image_url)) {
    const img = document.createElement("img");
    img.src = c.image_url;
    div.appendChild(img);
  }

  const body = document.createElement("div");
  body.className = "body";

  const titleWrap = document.createElement("div");
  const link = document.createElement("a");
  link.textContent = c.title;
  if (isHttp(c.url)) {
    link.href = c.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  titleWrap.appendChild(link);
  const badge = document.createElement("span");
  badge.className = "source-badge";
  badge.textContent = SOURCE_LABELS[c.source] || c.source;
  titleWrap.appendChild(badge);
  if (c.model) {
    const modelBadge = document.createElement("span");
    modelBadge.className = "model-badge";
    modelBadge.textContent = c.model;
    modelBadge.title = "AI model that judged this listing";
    titleWrap.appendChild(modelBadge);
  }
  if (c.ended) {
    div.classList.add("ended");
    const endedBadge = document.createElement("span");
    endedBadge.className = "ended-badge";
    endedBadge.textContent = "Ended";
    titleWrap.appendChild(endedBadge);
  }
  if (c.backfill) {
    div.classList.add("backfill");
    const bf = document.createElement("span");
    bf.className = "backfill-badge";
    bf.textContent = "Below bar";
    bf.title = "Shown to keep the feed active \u2014 scored just under the bar";
    titleWrap.appendChild(bf);
  }
  // Shows an existing rating; mainly relevant in browse mode.
  const verdictBadge = document.createElement("span");
  verdictBadge.className = "verdict-badge";
  if (c.verdict_label) {
    verdictBadge.textContent = c.verdict_label;
  } else {
    verdictBadge.hidden = true;
  }
  titleWrap.appendChild(verdictBadge);
  body.appendChild(titleWrap);

  body.appendChild(makeDiv("scores",
    `deal ${c.deal_score ?? "?"}/100 - fit ${c.fit_score ?? "?"}/100 - ${c.condition}`));
  body.appendChild(makeDiv("price", `${c.price} - est. value ${c.value}`));
  body.appendChild(makeDiv("delivery", c.delivery));
  body.appendChild(makeDiv("summary", c.summary));

  const buttons = document.createElement("div");
  buttons.className = "buttons";
  for (const b of c.buttons) {
    const btn = document.createElement("button");
    btn.dataset.verdict = b.verdict;
    btn.textContent = b.label;
    btn.addEventListener("click", async () => {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uid: c.uid, verdict: b.verdict }),
      });
      if (browse) {
        verdictBadge.textContent = b.label;
        verdictBadge.hidden = false;
      } else {
        div.remove();
      }
    });
    buttons.appendChild(btn);
  }
  if (!browse) {
    // Dismiss: leave the feed without recording a verdict, so a "no opinion"
    // hide doesn't skew the taste model.
    const dismiss = document.createElement("button");
    dismiss.className = "dismiss";
    dismiss.textContent = "\u2715 Dismiss";
    dismiss.title = "Hide without rating (won't affect taste learning)";
    dismiss.addEventListener("click", async () => {
      await fetch("/api/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uid: c.uid }),
      });
      div.remove();
    });
    buttons.appendChild(dismiss);
  }
  body.appendChild(buttons);

  div.appendChild(body);
  return div;
}

function applyFilter() {
  const selected = checkedSources();
  const item = document.getElementById("item-filter").value;
  const root = document.getElementById("cards");
  const empty = document.getElementById("empty");
  let visible = 0;
  for (const el of root.children) {
    // No boxes checked -> show all (checkboxes not yet loaded, or user cleared).
    const sourceOk = selected === null || selected.has(el.dataset.source);
    const itemOk = item === "" || el.dataset.watchItem === item;
    const show = sourceOk && itemOk;
    el.style.display = show ? "" : "none";
    if (show) visible++;
  }
  empty.style.display = visible ? "none" : "block";
}

// The set of checked source names, or null when the checkboxes aren't ready.
function checkedSources() {
  const boxes = document.querySelectorAll("#source-filters input[type=checkbox]");
  if (!boxes.length) return null;
  const on = new Set();
  for (const b of boxes) if (b.checked) on.add(b.value);
  return on;
}

async function buildSourceFilters() {
  let names = [];
  try {
    const res = await fetch("/api/sources");
    names = (await res.json()).sources || [];
  } catch (e) {
    return;
  }
  const root = document.getElementById("source-filters");
  root.textContent = "";
  for (const name of names) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = name;
    box.checked = true;
    box.addEventListener("change", applyFilter);
    label.appendChild(box);
    const span = document.createElement("span");
    span.textContent = SOURCE_LABELS[name] || name;
    label.appendChild(span);
    root.appendChild(label);
  }
}

function makeDiv(className, text) {
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  return el;
}

function isHttp(url) {
  try {
    const u = new URL(url, window.location.href);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}
buildSourceFilters();
refresh();
// Only auto-refresh the pending feed; browse mode is a manual snapshot.
setInterval(() => { if (!browseItem) refresh(); }, 5000);

document.getElementById("browse-btn").addEventListener("click", () => {
  if (browseItem) {
    exitBrowse();
  } else {
    const item = document.getElementById("item-filter").value;
    if (item) loadBrowse(item);
  }
});

const sendMoreBtn = document.getElementById("send-more");
const sendMoreStatus = document.getElementById("send-more-status");
const clearAllBtn = document.getElementById("clear-all");

clearAllBtn.addEventListener("click", async () => {
  if (!confirm("Clear all queued and visible offers?")) return;
  clearAllBtn.disabled = true;
  try {
    const res = await fetch("/api/clear_all", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Clear failed.");
    sendMoreStatus.textContent = data.message || "Dashboard cleared.";
    if (browseItem) exitBrowse(); else refresh();
  } catch (e) {
    sendMoreStatus.textContent = e?.message || "Clear failed.";
  } finally {
    clearAllBtn.disabled = false;
  }
});

function pollProgress(statusEl) {
  // Agile feedback while a "send more"/"search now" run is in flight: a poll
  // can take 10-60s, so poll /api/progress for short status updates (source
  // X done, analyzing listing Y, ...) instead of leaving a static message up.
  return setInterval(async () => {
    try {
      const res = await fetch("/api/progress");
      if (!res.ok) return;
      const data = await res.json();
      if (data.message) statusEl.textContent = data.message;
    } catch (e) {
      // Transient poll failures aren't worth surfacing; the next tick retries.
    }
  }, 700);
}

sendMoreBtn.addEventListener("click", async () => {
  sendMoreBtn.disabled = true;
  sendMoreStatus.textContent = "Starting search\u2026";
  const poll = pollProgress(sendMoreStatus);
  try {
    const selected = checkedSources();
    const body = selected ? JSON.stringify({ sources: Array.from(selected) }) : null;
    const res = await fetch("/api/send_more", {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body,
    });
    const data = await res.json();
    sendMoreStatus.textContent = data.message;
  } catch (e) {
    sendMoreStatus.textContent = "Request failed.";
  } finally {
    clearInterval(poll);
    sendMoreBtn.disabled = false;
    refresh();
  }
});

// --- Watchlist panel ------------------------------------------------------
const PRIORITIES = ["low", "normal", "high"];

function fieldsToItem(fields) {
  const splitList = (s) => s.split(",").map(t => t.trim()).filter(Boolean);
  const item = {
    name: fields.name.value.trim(),
    queries: splitList(fields.queries.value),
    keywords: splitList(fields.keywords.value),
    notes: fields.notes.value.trim(),
    priority: fields.priority.value,
  };
  const lo = fields.min_price.value.trim();
  const hi = fields.max_price.value.trim();
  if (lo !== "") item.min_price = Number(lo);
  if (hi !== "") item.max_price = Number(hi);
  return item;
}

function labeledInput(labelText, value, opts = {}) {
  const label = document.createElement("label");
  label.appendChild(document.createTextNode(labelText));
  const input = opts.textarea ? document.createElement("textarea") : document.createElement("input");
  if (opts.type) input.type = opts.type;
  input.value = value == null ? "" : value;
  if (opts.placeholder) input.placeholder = opts.placeholder;
  label.appendChild(input);
  return { label, input };
}

function buildEditor(item, { onSave, onCancel }) {
  const form = document.createElement("div");
  form.className = "watch-form";
  const name = labeledInput("Name", item.name, { placeholder: "Short name" });
  const queries = labeledInput("Search queries (comma-separated)", (item.queries || []).join(", "));
  const keywords = labeledInput("Keywords (comma-separated)", (item.keywords || []).join(", "));
  const notes = labeledInput("Notes for the AI", item.notes || "", { textarea: true });
  const minP = labeledInput("Min price (SEK)", item.min_price ?? "", { type: "number" });
  const maxP = labeledInput("Max price (SEK)", item.max_price ?? "", { type: "number" });

  const prioLabel = document.createElement("label");
  prioLabel.appendChild(document.createTextNode("Priority"));
  const prio = document.createElement("select");
  for (const p of PRIORITIES) {
    const opt = document.createElement("option");
    opt.value = p; opt.textContent = p;
    if ((item.priority || "normal") === p) opt.selected = true;
    prio.appendChild(opt);
  }
  prioLabel.appendChild(prio);

  const priceRow = document.createElement("div");
  priceRow.className = "row";
  priceRow.append(minP.label, maxP.label, prioLabel);

  const status = document.createElement("span");
  status.id = "watch-status";
  const actions = document.createElement("div");
  actions.className = "row";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  actions.append(saveBtn, cancelBtn, status);

  const fields = {
    name: name.input, queries: queries.input, keywords: keywords.input,
    notes: notes.input, min_price: minP.input, max_price: maxP.input, priority: prio,
  };
  saveBtn.addEventListener("click", async () => {
    const payload = fieldsToItem(fields);
    if (!payload.name) { status.textContent = "Name is required."; return; }
    if (!payload.queries.length) { status.textContent = "Add at least one search query."; return; }
    saveBtn.disabled = true;
    status.textContent = "Saving...";
    try {
      await onSave(payload, status);
    } finally {
      saveBtn.disabled = false;
    }
  });
  cancelBtn.addEventListener("click", onCancel);

  form.append(name.label, queries.label, keywords.label, notes.label, priceRow, actions);
  return form;
}

function closeEditor() {
  document.getElementById("watch-editor").textContent = "";
}

function openEditor(item, existingName) {
  const editor = document.getElementById("watch-editor");
  editor.textContent = "";
  editor.appendChild(buildEditor(item, {
    onCancel: closeEditor,
    onSave: async (payload, status) => {
      const isEdit = existingName != null;
      const url = isEdit ? "/api/watchlist/" + encodeURIComponent(existingName) : "/api/watchlist";
      const res = await fetch(url, {
        method: isEdit ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        status.textContent = data.error || "Save failed.";
        return;
      }
      closeEditor();
      loadWatchlist();
    },
  }));
}

function renderWatchItem(item) {
  const div = document.createElement("div");
  div.className = "watch-item";
  const head = document.createElement("div");
  const name = document.createElement("span");
  name.className = "watch-name";
  name.textContent = item.name;
  head.appendChild(name);
  if (item.priority && item.priority !== "normal") {
    const prio = document.createElement("span");
    prio.className = "watch-prio";
    prio.textContent = item.priority;
    head.appendChild(prio);
  }
  div.appendChild(head);

  if (item.queries && item.queries.length) {
    div.appendChild(makeDiv("watch-meta", "Queries: " + item.queries.join(", ")));
  }
  const bounds = [];
  if (item.min_price != null) bounds.push("min " + item.min_price + " SEK");
  if (item.max_price != null) bounds.push("max " + item.max_price + " SEK");
  if (bounds.length) div.appendChild(makeDiv("watch-meta", bounds.join(" \u00b7 ")));

  const actions = document.createElement("div");
  actions.className = "watch-actions";
  const searchBtn = document.createElement("button");
  searchBtn.type = "button";
  searchBtn.textContent = "\U0001f50d Search now";
  searchBtn.addEventListener("click", async () => {
    searchBtn.disabled = true;
    const status = document.getElementById("send-more-status");
    status.textContent = "Starting search for '" + item.name + "'\u2026";
    const poll = pollProgress(status);
    try {
      const res = await fetch("/api/watchlist/" + encodeURIComponent(item.name) + "/search", { method: "POST" });
      const data = await res.json();
      status.textContent = data.message || "";
      // Focus the feed on this item's results.
      const filter = document.getElementById("item-filter");
      filter.value = item.name;
      await refresh();
      applyFilter();
    } catch (e) {
      status.textContent = "Search failed.";
    } finally {
      clearInterval(poll);
      searchBtn.disabled = false;
    }
  });
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Edit";
  edit.addEventListener("click", () => openEditor(item, item.name));
  const del = document.createElement("button");
  del.type = "button";
  del.className = "danger";
  del.textContent = "Delete";
  del.addEventListener("click", async () => {
    if (!confirm("Remove '" + item.name + "' from the watchlist?")) return;
    const res = await fetch("/api/watchlist/" + encodeURIComponent(item.name), { method: "DELETE" });
    if (res.ok) loadWatchlist();
  });
  actions.append(searchBtn, edit, del);
  div.appendChild(actions);
  return div;
}

function populateItemFilter(items) {
  const filter = document.getElementById("item-filter");
  const current = filter.value;
  filter.textContent = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All items";
  filter.appendChild(all);
  for (const item of items) {
    const opt = document.createElement("option");
    opt.value = item.name;
    opt.textContent = item.name;
    filter.appendChild(opt);
  }
  // Preserve the selection if that item still exists.
  filter.value = items.some(i => i.name === current) ? current : "";
  updateModeUI();
}

async function loadWatchlist() {
  let items = [];
  try {
    const res = await fetch("/api/watchlist");
    items = (await res.json()).items || [];
  } catch (e) {
    return;
  }
  const root = document.getElementById("watch-items");
  root.textContent = "";
  if (!items.length) {
    root.appendChild(makeDiv("watch-meta", "Nothing on the watchlist yet."));
  }
  for (const item of items) root.appendChild(renderWatchItem(item));
  populateItemFilter(items);
  applyFilter();
}

document.getElementById("item-filter").addEventListener("change", () => {
  if (browseItem) {
    // Switching the item while browsing reloads the browse view (or exits it
    // when "All items" is chosen, since browse is per-item).
    const item = document.getElementById("item-filter").value;
    if (item) loadBrowse(item); else exitBrowse();
  } else {
    updateModeUI();
    applyFilter();
  }
});

async function loadTelegramState() {
  try {
    const res = await fetch("/api/telegram");
    const data = await res.json();
    const wrap = document.getElementById("mute-wrap");
    if (!data.available) { wrap.hidden = true; return; }
    wrap.hidden = false;
    document.getElementById("mute-toggle").checked = !!data.muted;
  } catch (e) { /* leave hidden */ }
}

document.getElementById("mute-toggle").addEventListener("change", async (e) => {
  const muted = e.target.checked;
  try {
    await fetch("/api/telegram/mute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ muted }),
    });
  } catch (err) { /* best-effort */ }
});

const draftBtn = document.getElementById("draft-btn");
const draftStatus = document.getElementById("draft-status");
draftBtn.addEventListener("click", async () => {
  const text = document.getElementById("draft-text").value.trim();
  if (!text) { draftStatus.textContent = "Describe what to watch for first."; return; }
  draftBtn.disabled = true;
  draftStatus.textContent = "Drafting...";
  try {
    const res = await fetch("/api/watchlist/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) { draftStatus.textContent = data.error || "Draft failed."; return; }
    draftStatus.textContent = "Review the draft below, then Save.";
    openEditor(data.item, null);
  } catch (e) {
    draftStatus.textContent = "Draft failed.";
  } finally {
    draftBtn.disabled = false;
  }
});

document.getElementById("add-blank-btn").addEventListener("click", () => {
  openEditor({ priority: "normal" }, null);
});

async function loadLegend() {
  let verdicts = [];
  try {
    const res = await fetch("/api/verdicts");
    verdicts = (await res.json()).verdicts || [];
  } catch (e) {
    return;
  }
  const root = document.getElementById("legend-list");
  root.textContent = "";
  for (const v of verdicts) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "legend-label";
    label.textContent = v.label;
    const desc = document.createElement("span");
    desc.className = "legend-desc";
    desc.textContent = v.description;
    li.append(label, desc);
    root.appendChild(li);
  }
}

loadWatchlist();
loadTelegramState();
loadLegend();

(async () => {
  try {
    const res = await fetch("/api/version");
    const data = await res.json();
    const el = document.getElementById("version-hash");
    if (el && data.git_hash) el.textContent = data.git_hash;
  } catch (e) { /* best-effort */ }
})();
</script>
<footer style="margin-top:2rem;color:#555;font-size:0.75rem;">git <span id="version-hash"></span></footer>
</body>
</html>
"""


class Dashboard:
    """Background HTTP server backed by the shared SQLite database.

    Acts as a notification channel in its own right (``send_listing`` queues a
    deal onto the feed), so the pipeline routes every qualifying deal here.
    """

    def __init__(self, db: Database, logistics: Logistics, host: str, port: int,
                 min_visible: int = 8) -> None:
        self._db = db
        self._logistics = logistics
        self._host = host
        self._port = port
        # Keep at least this many cards on the pending feed, backfilling with the
        # best below-bar listings when too few real deals are waiting.
        self._min_visible = min_visible
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._trigger: ManualTrigger | None = None
        self._pipeline: Pipeline | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Names of the enabled sources, shown as filter checkboxes. Set once the
        # pipeline's sources are known (see set_sources).
        self._source_names: list[str] = []
        # Latest "send more"/"search now" progress message, polled by the
        # browser via /api/progress while a run is in flight. Written from the
        # asyncio loop thread (the pipeline's on_progress callback) and read
        # from worker threads handling GET requests, hence the lock.
        self._progress_lock = threading.Lock()
        self._progress_message = ""

    def set_progress(self, message: str) -> None:
        """Record the latest agile status update for a run in progress."""

        with self._progress_lock:
            self._progress_message = message

    def get_progress(self) -> str:
        with self._progress_lock:
            return self._progress_message

    def attach_trigger(self, trigger: ManualTrigger) -> None:
        """Wire up the shared 'send more' trigger once the pipeline exists."""

        self._trigger = trigger

    def attach_pipeline(self, pipeline: Pipeline) -> None:
        """Wire up the pipeline so the watchlist panel can read/edit it."""

        self._pipeline = pipeline

    def set_sources(self, names: list[str]) -> None:
        """Record which sources are enabled, for the filter checkboxes."""

        self._source_names = list(names)

    async def send_listing(self, listing: Listing, _analysis: DealAnalysis) -> None:
        self._db.queue_for_dashboard(listing.uid)
        self._db.mark_notified(listing.uid)
        log.info("Queued %s for the local dashboard", listing.uid)

    def start(self) -> None:
        # Must be called from within the running asyncio loop so /api/send_more
        # (handled on a worker thread) can schedule pipeline runs back onto it.
        self._loop = asyncio.get_running_loop()
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                log.debug("dashboard: " + format, *args)

            def handle_one_request(self) -> None:
                # A client that navigates away or times out while we're still
                # writing (e.g. a slow /api/send_more response) drops the socket
                # mid-write. That surfaces as BrokenPipeError/ConnectionReset in
                # the worker thread and otherwise dumps a scary traceback for a
                # completely benign disconnect. Swallow it and close quietly.
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def _send_json(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_source_filter(self) -> list[str] | None:
                """Parse an optional {"sources": [...]} body for send_more.

                Returns the validated subset of known source names, or ``None``
                to poll all sources (empty/absent selection, or a malformed
                body). Unknown names are dropped so a crafted request can't
                widen the poll beyond the configured sources.
                """
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return None
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    requested = payload.get("sources")
                except (ValueError, AttributeError):
                    return None
                if not isinstance(requested, list):
                    return None
                known = set(dashboard._source_names)
                selected = [s for s in requested if isinstance(s, str) and s in known]
                # An empty/all selection means "no restriction".
                if not selected or set(selected) == known:
                    return None
                return selected

            def _read_json_body(self) -> object | None:
                """Read and parse a JSON request body, or None if malformed."""

                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return None
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return None

            def _run_pipeline_coro(self, coro, *, timeout: float = 120):
                """Run a pipeline coroutine on the asyncio loop from this worker.

                Returns ``(ok, result_or_exc)``. Watchlist edits go through the
                pipeline so they're serialized against each other and Git
                synchronization (see the pipeline's watchlist lock); the
                dashboard runs on worker threads, so we hand the coroutine back
                to the loop via run_coroutine_threadsafe.

                A poll already in flight holds _run_lock for as long as the
                sweep takes, so /api/send_more may wait that long. If we do time
                out, cancel the future so the operation can't still land in the
                background after the client has already been told it failed.
                """

                if dashboard._pipeline is None or dashboard._loop is None:
                    return False, RuntimeError("pipeline not ready")
                future = asyncio.run_coroutine_threadsafe(coro, dashboard._loop)
                try:
                    return True, future.result(timeout=timeout)
                except Exception as exc:  # noqa: BLE001 - report, don't crash handler
                    future.cancel()
                    return False, exc

            def _pipeline_ready(self) -> bool:
                """503 the request if the pipeline isn't wired up yet."""

                if dashboard._pipeline is not None and dashboard._loop is not None:
                    return True
                self._send_json(503, {"error": "Not ready yet \u2014 try again in a moment."})
                return False

            def _parse_watch_item(self) -> WatchItem | None:
                """Validate a WatchItem from the JSON body, or 400 and return None."""

                body = self._read_json_body()
                if not isinstance(body, dict):
                    self._send_json(400, {"error": "Expected a JSON object."})
                    return None
                try:
                    return WatchItem.model_validate(body)
                except ValidationError as exc:
                    self._send_json(400, {"error": exc.errors()[0].get("msg", "Invalid item.")})
                    return None

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path in ("/", "/index.html"):
                    body = _PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/pending":
                    cards = [
                        _card(
                            listing,
                            dashboard._db.get_analysis(listing.uid),
                            dashboard._logistics,
                        )
                        for listing in dashboard._db.dashboard_pending()
                    ]
                    # Backfill with the best still-open listings that fell just
                    # under the bar (and were never surfaced, rated or dismissed)
                    # so the feed is never empty in a quiet market. Flagged as
                    # backfill so the UI can mark them "below bar".
                    deficit = dashboard._min_visible - len(cards)
                    if deficit > 0:
                        for listing in dashboard._db.top_unseen_listings():
                            if listing.is_ended():
                                continue
                            cards.append(
                                _card(
                                    listing,
                                    dashboard._db.get_analysis(listing.uid),
                                    dashboard._logistics,
                                    backfill=True,
                                )
                            )
                            deficit -= 1
                            if deficit == 0:
                                break
                    self._send_json(200, cards)
                    return
                if self.path.startswith("/api/active"):
                    params = parse_qs(urlparse(self.path).query)
                    item = (params.get("item") or [""])[0].strip()
                    if not item:
                        self._send_json(400, {"error": "An item is required."})
                        return
                    # Every offer seen for this item that is still open, most
                    # recently seen first, tagged with any verdict already given.
                    cards = [
                        _card(
                            listing,
                            dashboard._db.get_analysis(listing.uid),
                            dashboard._logistics,
                            dashboard._db.get_feedback(listing.uid),
                        )
                        for listing in dashboard._db.listings_for_watch_item(item)
                        if not listing.is_ended()
                    ]
                    self._send_json(200, cards)
                    return
                if self.path == "/api/sources":
                    self._send_json(200, {"sources": dashboard._source_names})
                    return
                if self.path == "/api/progress":
                    self._send_json(200, {"message": dashboard.get_progress()})
                    return
                if self.path == "/api/version":
                    self._send_json(200, {"git_hash": _GIT_HASH})
                    return
                if self.path == "/api/verdicts":
                    self._send_json(200, {
                        "verdicts": [
                            {
                                "verdict": v.value,
                                "label": VERDICT_LABELS[v],
                                "description": VERDICT_DESCRIPTIONS[v],
                            }
                            for v in VERDICT_LABELS
                        ]
                    })
                    return
                if self.path == "/api/watchlist":
                    if dashboard._pipeline is None:
                        self._send_json(200, {"items": []})
                        return
                    watchlist = dashboard._pipeline.get_watchlist()
                    items = [it.model_dump() for it in watchlist.items]
                    self._send_json(200, {"items": items})
                    return
                if self.path == "/api/telegram":
                    pipeline = dashboard._pipeline
                    self._send_json(200, {
                        "available": pipeline.telegram_available if pipeline else False,
                        "muted": pipeline.alerts_muted if pipeline else False,
                    })
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path == "/api/clear_all":
                    if not self._pipeline_ready():
                        return
                    ok, result = self._run_pipeline_coro(dashboard._pipeline.clear_dashboard())
                    if not ok:
                        self._send_json(500, {"error": "Could not clear the dashboard."})
                        return
                    self._send_json(200, {"message": f"Cleared {result} offer(s)."})
                    return
                if self.path == "/api/send_more":
                    if dashboard._trigger is None or dashboard._loop is None:
                        self._send_json(
                            503, {"message": "Not ready yet \u2014 try again in a moment."}
                        )
                        return
                    # Optional {"sources": [...]} restricts the poll to the
                    # checked sources; validate against the known set so a
                    # crafted body can't inject unknown source names.
                    source_names = self._read_source_filter()
                    dashboard.set_progress("Starting search…")
                    future = asyncio.run_coroutine_threadsafe(
                        dashboard._trigger.trigger(
                            source_names, on_progress=dashboard.set_progress
                        ),
                        dashboard._loop,
                    )
                    try:
                        message = future.result(timeout=120)
                    except Exception as exc:  # noqa: BLE001 - report, don't crash the handler
                        log.warning("Manual trigger failed: %s", exc)
                        message = "Something went wrong checking for deals."
                    dashboard.set_progress("")
                    self._send_json(200, {"message": message})
                    return
                if self.path == "/api/watchlist/suggest":
                    if not self._pipeline_ready():
                        return
                    body = self._read_json_body()
                    text = body.get("text", "") if isinstance(body, dict) else ""
                    if not isinstance(text, str) or not text.strip():
                        self._send_json(400, {"error": "Describe what to watch for."})
                        return
                    ok, result = self._run_pipeline_coro(
                        dashboard._pipeline.suggest_watch_item(text), timeout=60
                    )
                    if not ok:
                        log.warning("Watchlist AI draft failed: %s", result)
                        self._send_json(502, {"error": "The AI couldn't draft that."})
                        return
                    self._send_json(200, {"item": result})
                    return
                if self.path == "/api/watchlist":
                    if not self._pipeline_ready():
                        return
                    item = self._parse_watch_item()
                    if item is None:
                        return
                    ok, result = self._run_pipeline_coro(
                        dashboard._pipeline.add_watch_item(item)
                    )
                    if not ok:
                        self._send_json(409, {"error": str(result)})
                        return
                    self._send_json(200, {"item": item.model_dump()})
                    return
                if self.path == "/api/telegram/mute":
                    if dashboard._pipeline is None:
                        self._send_json(503, {"error": "Not ready yet."})
                        return
                    body = self._read_json_body()
                    muted = bool(body.get("muted")) if isinstance(body, dict) else False
                    dashboard._pipeline.set_alerts_muted(muted)
                    self._send_json(200, {"muted": muted})
                    return
                item_search = self._watch_item_name(suffix="/search")
                if item_search is not None:
                    if dashboard._trigger is None or dashboard._loop is None:
                        self._send_json(503, {"message": "Not ready yet."})
                        return
                    known = {it.name for it in dashboard._pipeline.get_watchlist().items}
                    if item_search not in known:
                        self._send_json(404, {"message": f"No watch item {item_search!r}."})
                        return
                    dashboard.set_progress(f"Starting search for '{item_search}'…")
                    future = asyncio.run_coroutine_threadsafe(
                        dashboard._trigger.trigger(
                            watch_item=item_search, on_progress=dashboard.set_progress
                        ),
                        dashboard._loop,
                    )
                    try:
                        message = future.result(timeout=120)
                    except Exception as exc:  # noqa: BLE001 - report, don't crash the handler
                        log.warning("Item search failed: %s", exc)
                        message = "Something went wrong checking for deals."
                    dashboard.set_progress("")
                    self._send_json(200, {"message": message})
                    return
                if self.path == "/api/dismiss":
                    # Remove a card from the pending feed WITHOUT recording a
                    # verdict, so a "no opinion" hide doesn't skew taste learning.
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        payload = json.loads(self.rfile.read(length) or b"{}")
                        uid = str(payload["uid"])
                    except (ValueError, KeyError, TypeError):
                        self.send_response(400)
                        self.end_headers()
                        return
                    dashboard._db.dequeue_from_dashboard(uid)
                    dashboard._db.mark_dismissed(uid)
                    log.info("Dashboard dismiss (no verdict) for %s", uid)
                    self._send_json(200, {"ok": True})
                    return
                if self.path != "/api/feedback":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    uid = str(payload["uid"])
                    verdict = Verdict(payload["verdict"])
                except (ValueError, KeyError, TypeError):
                    self.send_response(400)
                    self.end_headers()
                    return
                dashboard._db.save_feedback(Feedback(listing_uid=uid, verdict=verdict))
                dashboard._db.dequeue_from_dashboard(uid)
                log.info("Dashboard feedback %s for %s", verdict.value, uid)
                self._send_json(200, {"ok": True})

            def _watch_item_name(self, suffix: str = "") -> str | None:
                """Extract {name} from /api/watchlist/{name}{suffix}, or None.

                With ``suffix="/search"`` it matches the per-item search route
                and won't collide with the plain PUT/DELETE /api/watchlist/{name}.
                """

                prefix = "/api/watchlist/"
                if not self.path.startswith(prefix):
                    return None
                rest = self.path[len(prefix):]
                if suffix:
                    if not rest.endswith(suffix):
                        return None
                    rest = rest[: -len(suffix)]
                elif "/" in rest:
                    # A sub-resource like {name}/search: not a plain item route.
                    return None
                name = unquote(rest)
                return name or None

            def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
                name = self._watch_item_name()
                if name is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if not self._pipeline_ready():
                    return
                item = self._parse_watch_item()
                if item is None:
                    return
                ok, result = self._run_pipeline_coro(
                    dashboard._pipeline.update_watch_item(name, item)
                )
                if not ok:
                    status = 404 if isinstance(result, KeyError) else 409
                    self._send_json(status, {"error": f"Could not update {name!r}."})
                    return
                self._send_json(200, {"item": item.model_dump()})

            def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
                name = self._watch_item_name()
                if name is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if not self._pipeline_ready():
                    return
                ok, result = self._run_pipeline_coro(
                    dashboard._pipeline.remove_watch_item(name)
                )
                if not ok:
                    status = 404 if isinstance(result, KeyError) else 500
                    self._send_json(status, {"error": f"Could not remove {name!r}."})
                    return
                self._send_json(200, {"ok": True})

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("Local dashboard listening on http://%s:%d", self._host, self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
