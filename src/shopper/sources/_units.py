"""Imperial → metric unit annotation for marketplace text.

eBay's US/UK marketplaces describe tools in inches, feet, pounds and ounces.
The buyer thinks in metric, so we append a converted value in parentheses right
after each imperial measurement — e.g. ``6" (15.2 cm)``, ``10 lb (4.5 kg)`` —
without touching the original text. Conversions are skipped when a metric value
already follows, so re-runs and already-metric listings stay clean.
"""

from __future__ import annotations

import re

# Fractions like ½ appear in tool sizes; map the common ones to decimals.
_VULGAR_FRACTIONS = {
    "¼": 0.25,
    "½": 0.5,
    "¾": 0.75,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "⅛": 0.125,
    "⅜": 0.375,
    "⅝": 0.625,
    "⅞": 0.875,
}

# A number that may be a decimal, a simple fraction (1/2), a mixed number
# (6-1/2 or 6 1/2) or a unicode vulgar fraction (6½).
_NUM = r"\d+(?:\.\d+)?(?:\s*[-\s]\s*\d+\s*/\s*\d+)?|\d+\s*/\s*\d+|\d*\s*[¼½¾⅓⅔⅛⅜⅝⅞]"


def _parse_number(raw: str) -> float | None:
    """Parse a decimal / fraction / mixed-number / vulgar-fraction string."""

    text = raw.strip()
    if not text:
        return None

    # Split off a trailing vulgar fraction (e.g. "6½").
    total = 0.0
    for symbol, value in _VULGAR_FRACTIONS.items():
        if symbol in text:
            text = text.replace(symbol, "").strip().rstrip("-").strip()
            total += value
            break

    if text:
        # A mixed number uses a '-' or space between the whole and the fraction;
        # a bare fraction has no whole part.
        text = text.replace(" ", "")
        if "/" in text:
            whole = 0.0
            frac = text
            if "-" in text:
                whole_str, frac = text.split("-", 1)
                whole = float(whole_str) if whole_str else 0.0
            num, den = frac.split("/", 1)
            try:
                total += whole + float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                return None
        else:
            try:
                total += float(text)
            except ValueError:
                return None
    return total


def _fmt(value: float) -> str:
    """Format a converted metric value without trailing noise."""

    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def _cm(inches: float) -> str:
    return f"{_fmt(inches * 2.54)} cm"


def _cm_from_feet(feet: float) -> str:
    return f"{_fmt(feet * 30.48)} cm"


def _kg_or_g(pounds: float) -> str:
    grams = pounds * 453.59237
    if grams < 1000:
        return f"{_fmt(grams)} g"
    return f"{_fmt(grams / 1000)} kg"


def _g(ounces: float) -> str:
    return f"{_fmt(ounces * 28.349523)} g"


# Each rule: a regex matching "<number><unit>" and a converter for the number.
# The unit alternatives are ordered longest-first so "inch" wins over "in".
# ``(?![\w])`` avoids matching inside larger words. The bare space-separated
# "in" is deliberately excluded — it collides with the English word "in" (e.g.
# "6 in stock") — so inches require a quote, "inch(es)", "in." or a glued "6in".
_RULES: list[tuple[re.Pattern[str], object]] = [
    (
        re.compile(
            rf"(?P<n>{_NUM})\s*(?:inches|inch|in\.|\")(?![\w])|(?P<n2>{_NUM})in(?![\w])",
            re.IGNORECASE,
        ),
        _cm,
    ),
    (
        re.compile(rf"(?P<n>{_NUM})\s*(?:feet|foot|ft\.|ft|')(?![\w])", re.IGNORECASE),
        _cm_from_feet,
    ),
    (
        re.compile(rf"(?P<n>{_NUM})\s*(?:pounds|pound|lbs|lb)(?![\w])", re.IGNORECASE),
        _kg_or_g,
    ),
    (
        re.compile(rf"(?P<n>{_NUM})\s*(?:ounces|ounce|oz)(?![\w])", re.IGNORECASE),
        _g,
    ),
]

# If a metric value already follows within a parenthesis, don't add another.
_ALREADY_METRIC = re.compile(r"^\s*\((?:[^)]*\d)", re.IGNORECASE)


def add_metric(text: str) -> str:
    """Return ``text`` with a metric value appended after each imperial one."""

    if not text:
        return text

    # Apply rules one at a time; each replacement runs over the current text so
    # positions stay valid between rules.
    for pattern, convert in _RULES:

        def _sub(match: re.Match[str], _convert=convert, _text=text) -> str:
            whole = match.group(0)
            # Skip when a metric annotation already follows this measurement.
            tail = _text[match.end() : match.end() + 12]
            if _ALREADY_METRIC.match(tail):
                return whole
            # The inch rule has two alternatives (n / n2); use whichever matched.
            raw = match.groupdict().get("n") or match.groupdict().get("n2")
            if raw is None:
                return whole
            value = _parse_number(raw)
            if value is None:
                return whole
            return f"{whole} ({_convert(value)})"

        text = pattern.sub(_sub, text)
    return text
