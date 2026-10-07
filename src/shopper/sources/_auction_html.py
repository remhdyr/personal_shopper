"""Small HTML helpers for best-effort auction source adapters."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

_TAG_RE = re.compile(r"<[^>]+>")


def attr(block: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}=[\"']([^\"']+)", block, re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def plain_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def class_text(block: str, class_name: str) -> str:
    match = re.search(
        rf"class=[\"'][^\"']*{re.escape(class_name)}[^\"']*[\"'][^>]*>(.*?)</",
        block,
        re.I | re.S,
    )
    return plain_text(match.group(1)) if match else ""


def absolute_url(base_url: str, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{base_url}{url}"
    return f"{base_url}/{url}"


def parse_sek_price(text: str) -> float | None:
    matches = re.findall(r"(\d[\d\s\xa0.]*)\s*(?:SEK|kr)\b", html.unescape(text), re.I)
    for raw in reversed(matches):
        normalized = raw.replace("\xa0", " ").replace(" ", "").replace(".", "")
        try:
            return float(normalized)
        except ValueError:
            continue
    return None


def parse_iso_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def in_price_range(price: float | None, min_price: float | None, max_price: float | None) -> bool:
    if price is None:
        return True
    if min_price is not None and price < min_price:
        return False
    if max_price is not None and price > max_price:
        return False
    return True


def query_matches(text: str, query_text: str) -> bool:
    needle = query_text.strip().lower()
    if not needle:
        return True
    haystack = text.lower()
    return all(part in haystack for part in needle.split())