"""Configuration loading.

Two layers:
- ``config.yaml``  -> behavioural settings (search terms, thresholds, cadence).
- ``.env`` / env   -> secrets (API keys, tokens) via :class:`Secrets`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class QuerySpec:
    """One marketplace search to run, with the price bounds that apply to it."""

    text: str
    min_price: float
    max_price: float
    # Watchlist item this query belongs to, so surfaced listings can be tagged
    # and filtered per item on the dashboard. None for legacy global queries.
    watch_item: str | None = None


class NotifyConfig(BaseModel):
    """Bar a scored listing must clear to appear on the hosted dashboard.

    These are the *dashboard* thresholds. They adapt to your feedback (see
    :class:`shopper.preferences.PreferenceEngine`); Telegram push alerts use the
    separate, stricter :class:`AlertsConfig` bar.
    """

    min_deal_score: int = Field(default=65, ge=0, le=100)
    min_fit_score: int = Field(default=55, ge=0, le=100)
    # Relax both bars automatically when the dashboard is running dry: each deal
    # short of this many pending lowers the bars a little (down to a sane floor),
    # so a ratcheted-up threshold or a quiet market can't leave the feed empty.
    # 0 disables the relief entirely.
    relief_target: int = Field(default=8, ge=0)


class AlertsConfig(BaseModel):
    """When to *push* a Telegram alert instead of only listing it on the dashboard.

    Telegram is reserved for the cream of the crop: an exceptional deal that was
    *just* posted, so you can act before someone else does. Everything that
    merely clears the dashboard bar still shows on the dashboard regardless.
    """

    enabled: bool = True
    # A "really good hit" must clear these (higher than the dashboard bar).
    min_deal_score: int = Field(default=80, ge=0, le=100)
    min_fit_score: int = Field(default=70, ge=0, le=100)
    # Only alert about listings first posted within this many minutes. Keep it
    # >= poll_interval_minutes so nothing slips through between polls. Sources
    # without a real posting timestamp fall back to "new to us since the last
    # poll", which is within one poll interval anyway.
    max_age_minutes: int = Field(default=15, ge=1)
    # Safety caps so a burst of great listings can't spam you. 0 means no cap.
    max_per_run: int = Field(default=5, ge=0)
    max_per_day: int = Field(default=30, ge=0)


class SearchConfig(BaseModel):
    """Global search envelope.

    ``queries`` and ``categories`` are a fallback used only when
    ``watchlist.yaml`` is empty; normally the watchlist is the source of truth
    for *what* to hunt for (see :class:`Watchlist`). The price bounds and
    location here always apply as a hard, global filter.
    """

    queries: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    min_price: float = Field(default=0, ge=0)
    max_price: float = Field(default=1_000_000, ge=0)
    location: str = ""
    # How many marketplace searches to run in parallel *per source* during a
    # poll. Searches are otherwise independent HTTP calls, so a handful in
    # flight at once turns a long sequential crawl into a quick burst without
    # hammering any single marketplace. Keep modest to stay clear of per-host
    # rate limits (429s).
    concurrency: int = Field(default=6, ge=1, le=50)


class WatchItem(BaseModel):
    """One thing the buyer is on the lookout for.

    Feeds both the marketplace queries (``queries``) and the AI's fit judgement
    (``name``/``keywords``/``notes``/``priority``/price ceiling).
    """

    name: str
    # Free-text terms actually searched against each marketplace.
    queries: list[str] = Field(default_factory=list)
    # Extra terms the AI should treat as strong matches (not searched directly).
    keywords: list[str] = Field(default_factory=list)
    # Guidance for the AI: preferred brands, what "good" looks like, red flags.
    notes: str = ""
    # Optional per-item price bounds. Narrow the marketplace query and tell the
    # AI what you're willing to pay; the global SearchConfig bounds still cap.
    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    priority: Literal["low", "normal", "high"] = "normal"


class Watchlist(BaseModel):
    """The buyer's wish list, loaded from a dedicated ``watchlist.yaml``."""

    # High-level categories the AI should treat as relevant.
    categories: list[str] = Field(default_factory=list)
    items: list[WatchItem] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> Watchlist:
        path = Path(path)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: str | Path) -> None:
        """Write the wish list back to ``path`` as YAML.

        Only non-default fields are dumped so the file stays terse; note that
        this *rewrites* the file, so any hand-written comments are lost (the
        dashboard is the source of truth once you start editing there).
        """

        path = Path(path)
        data = self.model_dump(exclude_defaults=True)
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


class InventoryItem(BaseModel):
    """Something the buyer already owns.

    Used by the AI two ways: to flag a listing that duplicates gear already owned
    (``duplicate_keywords``), and to boost items that are *compatible* tooling
    for it (``specs``/``compatible``) -- matching tapers, mounts and sizes.
    """

    name: str
    kind: str = ""  # e.g. lathe, mill, drill, measuring
    brand: str = ""
    model: str = ""
    notes: str = ""
    # Compatibility-critical values, kept as free-form strings ("MT3", "20 mm").
    specs: dict[str, str] = Field(default_factory=dict)
    # Add-ons already owned for this item -- treat matching listings as duplicates.
    accessories: list[str] = Field(default_factory=list)
    # Accessories/tooling that fit this item but you DON'T own yet -- fit boost.
    compatible: list[str] = Field(default_factory=list)
    # Terms that mean "this listing is the same machine we already have".
    duplicate_keywords: list[str] = Field(default_factory=list)


class Inventory(BaseModel):
    """What the buyer already owns, loaded from a dedicated ``inventory.yaml``."""

    items: list[InventoryItem] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> Inventory:
        path = Path(path)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)


class LogisticsConfig(BaseModel):
    """Where the buyer is and what it costs them to fetch an item by car.

    Pickup cost is a round trip: distance is one-way, so total driving is
    ``2 * distance_km``. Time is derived from ``avg_speed_kmh``.
    """

    home_city: str = "Malmö"
    car_cost_per_hour: float = Field(default=100.0, ge=0)
    car_cost_per_km: float = Field(default=2.0, ge=0)
    avg_speed_kmh: float = Field(default=80.0, gt=0)
    # Listings within this one-way distance are highlighted as "nearby".
    nearby_km: float = Field(default=50.0, ge=0)


class EbaySourceConfig(BaseModel):
    enabled: bool = True
    # eBay's Browse API doesn't support the Swedish marketplace; EBAY_DE (EUR)
    # is the closest EU market. Prices are converted to SEK automatically.
    marketplace: str = "EBAY_DE"
    # Only return used items. eBay is flooded with identical new stock from
    # thousands of retailers; this hunt is for second-hand kit, so restrict the
    # Browse query to used conditions (set False to include new/refurbished).
    used_only: bool = True


class AuctionetSourceConfig(BaseModel):
    """Auctionet online auctions. Public read API, so no credentials needed."""

    enabled: bool = True
    # Keep only lots in this currency (the app reasons in SEK end to end).
    currency: str = "SEK"


class ToggleSourceConfig(BaseModel):
    enabled: bool = False


class BlocketSourceConfig(ToggleSourceConfig):
    # Unknown seller types are also deferred when this is enabled, so a failed
    # detail fetch cannot accidentally admit a business listing.
    private_sellers_only: bool = True
    # Auction houses cross-post teaser ads to Blocket. Their own auction pages
    # have better bid and reserve-price data, so suppress those seller shops.
    excluded_sellers: list[str] = Field(
        default_factory=lambda: ["Blinto", "Klaravik", "PS Auction"]
    )


class SourcesConfig(BaseModel):
    ebay: EbaySourceConfig = Field(default_factory=EbaySourceConfig)
    tradera: ToggleSourceConfig = Field(default_factory=lambda: ToggleSourceConfig(enabled=True))
    auctionet: AuctionetSourceConfig = Field(default_factory=AuctionetSourceConfig)
    klaravik: ToggleSourceConfig = Field(default_factory=lambda: ToggleSourceConfig(enabled=True))
    blinto: ToggleSourceConfig = Field(default_factory=lambda: ToggleSourceConfig(enabled=True))
    psauction: ToggleSourceConfig = Field(default_factory=lambda: ToggleSourceConfig(enabled=True))
    blocket: BlocketSourceConfig = Field(default_factory=lambda: BlocketSourceConfig(enabled=True))
    # Retained for possible future repairs, but disabled by default because
    # their former public APIs are no longer available.
    fleasy: ToggleSourceConfig = Field(default_factory=ToggleSourceConfig)
    aterbygg: ToggleSourceConfig = Field(default_factory=ToggleSourceConfig)


class DashboardConfig(BaseModel):
    """The primary, always-on web UI — a browsable feed of every deal that
    clears the dashboard bar, each with the same like/dislike buttons as
    Telegram.

    It's meant to be reached over your private VPN (e.g. Tailscale), so it binds
    to ``0.0.0.0`` by default and relies on the VPN — not localhost — for access
    control. Set ``host`` to ``127.0.0.1`` to keep it strictly local.
    """

    enabled: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - intentional: reachable over the private VPN
    port: int = Field(default=8787, ge=1, le=65535)
    # Keep at least this many cards on the pending feed: when fewer real,
    # above-bar deals are waiting, the best still-open listings that scored just
    # under the bar are shown as "below bar" backfill so there's always something
    # to browse. 0 disables backfill (only real, above-bar deals show).
    min_visible: int = Field(default=8, ge=0)


class GitHubSyncConfig(BaseModel):
    """Synchronize dashboard-edited watchlists with the configured Git remote."""

    enabled: bool = False
    interval_minutes: int = Field(default=5, ge=1)
    remote: str = "origin"


class ProviderConfig(BaseModel):
    """One AI backend the load balancer may route analyses to.

    ``kind`` selects the transport: ``gemini`` (google-genai) or
    ``openai_compat`` (any OpenAI ``/chat/completions`` endpoint). ``rpm`` and
    ``daily_limit`` are the provider's free-tier caps the router throttles to
    (0 = unlimited); ``weight`` biases the capacity-weighted split when several
    providers have budget. The API key is never stored here — it comes from
    :class:`Secrets` keyed by ``name`` (e.g. ``gemini`` -> ``gemini_api_key``).
    """

    name: str
    kind: Literal["gemini", "openai_compat"] = "gemini"
    model: str = ""
    # Required for openai_compat; ignored for gemini.
    base_url: str = ""
    rpm: int = Field(default=15, ge=0)
    daily_limit: int = Field(default=0, ge=0)
    weight: float = Field(default=1.0, ge=0)


class AiConfig(BaseModel):
    """How to spread analyses across one or more AI providers."""

    mode: Literal["capacity_weighted", "round_robin"] = "capacity_weighted"
    providers: list[ProviderConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    """Behavioural configuration loaded from ``config.yaml``."""

    poll_interval_minutes: int = Field(default=10, ge=1)
    # Global safety valve across all providers. The per-provider daily_limits in
    # the ``ai`` section are the real budget enforcement; keep this high so it
    # never masks them, but non-zero so a misconfigured router can't run away.
    max_ai_analyses_per_day: int = Field(default=100_000, ge=0)
    # Cap AI calls in a single poll so a first run against a large backlog can't
    # burn the daily budget (or trip Gemini's per-minute limit) all at once. The
    # remainder is analyzed on later polls. 0 means no per-run cap.
    max_ai_analyses_per_run: int = Field(default=25, ge=0)
    dry_run: bool = True
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    logistics: LogisticsConfig = Field(default_factory=LogisticsConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    github_sync: GitHubSyncConfig = Field(default_factory=GitHubSyncConfig)
    # AI provider routing. When omitted from config.yaml, load() synthesises a
    # single Gemini provider so existing single-model setups keep working.
    ai: AiConfig = Field(default_factory=AiConfig)
    # Where the wish list lives, resolved relative to this config file.
    watchlist_file: str = "watchlist.yaml"
    watchlist: Watchlist = Field(default_factory=Watchlist)
    # Absolute path the watchlist was loaded from, so the dashboard can persist
    # edits back to the same file. Populated by :meth:`load`; None otherwise.
    watchlist_path: str | None = None
    # What the buyer already owns, resolved relative to this config file.
    inventory_file: str = "inventory.yaml"
    inventory: Inventory = Field(default_factory=Inventory)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> AppConfig:
        path = Path(path)
        if not path.exists():
            config = cls()
            config._default_ai_providers()
            return config
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        config._default_ai_providers()
        # Load the wish list and inventory from their own files, resolved next to
        # config.yaml so they're independent of the working directory.
        watchlist_path = cls._sibling(path, config.watchlist_file)
        config.watchlist = Watchlist.load(watchlist_path)
        config.watchlist_path = str(watchlist_path)
        config.inventory = Inventory.load(cls._sibling(path, config.inventory_file))
        return config

    @staticmethod
    def _sibling(config_path: Path, name: str) -> Path:
        """Resolve a companion file path relative to config.yaml's directory."""

        p = Path(name)
        return p if p.is_absolute() else config_path.parent / p

    def _default_ai_providers(self) -> None:
        """Synthesise a single Gemini provider when none are configured.

        Keeps single-model setups (no ``ai`` section in config.yaml) working:
        the provider's model is left blank so the pipeline falls back to
        ``Secrets.gemini_model``.
        """

        if not self.ai.providers:
            self.ai.providers = [ProviderConfig(name="gemini", kind="gemini", rpm=15)]

    def effective_categories(self) -> list[str]:
        """High-level relevance categories for the AI (watchlist wins)."""

        return self.watchlist.categories or self.search.categories

    def query_specs(self) -> list[QuerySpec]:
        """Every marketplace search to run, with its effective price bounds.

        Built from the watchlist (per-item price overrides fall back to the
        global bounds); if the watchlist has no items, the legacy
        ``search.queries`` are used with the global bounds. Duplicates are
        collapsed so overlapping watch items don't double-query.
        """

        specs: list[QuerySpec] = []
        seen: set[tuple[str, float, float]] = set()

        def add(text: str, lo: float, hi: float, item: str | None) -> None:
            key = (text, lo, hi)
            if key not in seen:
                seen.add(key)
                specs.append(QuerySpec(text=text, min_price=lo, max_price=hi, watch_item=item))

        if self.watchlist.items:
            for item in self.watchlist.items:
                lo = item.min_price if item.min_price is not None else self.search.min_price
                hi = item.max_price if item.max_price is not None else self.search.max_price
                for text in item.queries:
                    add(text, lo, hi, item.name)
        else:
            for text in self.search.queries:
                add(text, self.search.min_price, self.search.max_price, None)
        return specs


class Secrets(BaseSettings):
    """Secrets loaded from environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_token: str = ""
    telegram_chat_id: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"

    # Optional second AI provider (Qwen-VL via DashScope's OpenAI-compatible
    # endpoint). Dormant until a key is set; the router excludes keyless
    # providers, so leaving this blank simply keeps the service Gemini-only.
    qwen_api_key: str = ""

    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_env: str = "production"

    tradera_app_id: str = ""
    tradera_app_key: str = ""

    blocket_token: str = ""


def load() -> tuple[AppConfig, Secrets]:
    """Load both configuration layers."""

    return AppConfig.load(), Secrets()
