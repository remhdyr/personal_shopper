"""Tests for config loading and bounds validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shopper.config import AppConfig


def test_defaults_are_valid():
    config = AppConfig()
    assert config.logistics.avg_speed_kmh > 0
    assert 1 <= config.dashboard.port <= 65535
    assert config.github_sync.enabled is False
    assert config.github_sync.interval_minutes >= 1
    assert config.sources.blocket.private_sellers_only is True
    assert config.sources.fleasy.enabled is False
    assert config.sources.aterbygg.enabled is False


def test_load_missing_file_returns_defaults(tmp_path):
    config = AppConfig.load(tmp_path / "does-not-exist.yaml")
    # load() synthesises a default Gemini provider; otherwise it's the defaults.
    assert config.ai.providers and config.ai.providers[0].name == "gemini"
    config.ai.providers = []
    assert config == AppConfig()


def test_load_synthesises_gemini_provider_when_ai_omitted(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("poll_interval_minutes: 5\n", encoding="utf-8")
    config = AppConfig.load(path)
    assert [p.name for p in config.ai.providers] == ["gemini"]
    assert config.ai.providers[0].kind == "gemini"
    assert config.ai.mode == "capacity_weighted"


def test_load_parses_multi_provider_ai_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "ai:\n"
        "  mode: round_robin\n"
        "  providers:\n"
        "    - name: gemini\n"
        "      kind: gemini\n"
        "      rpm: 15\n"
        "    - name: qwen\n"
        "      kind: openai_compat\n"
        "      model: qwen-vl-max\n"
        "      base_url: https://example.com/v1\n"
        "      rpm: 60\n"
        "      weight: 2.0\n",
        encoding="utf-8",
    )
    config = AppConfig.load(path)
    assert config.ai.mode == "round_robin"
    assert [p.name for p in config.ai.providers] == ["gemini", "qwen"]
    qwen = config.ai.providers[1]
    assert qwen.kind == "openai_compat"
    assert qwen.base_url == "https://example.com/v1"
    assert qwen.weight == 2.0


def test_zero_avg_speed_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("logistics:\n  avg_speed_kmh: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        AppConfig.load(path)


def test_out_of_range_port_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("dashboard:\n  port: 70000\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        AppConfig.load(path)


def test_out_of_range_score_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notify:\n  min_deal_score: 150\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        AppConfig.load(path)


def test_valid_yaml_overrides_apply(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("poll_interval_minutes: 5\nnotify:\n  min_deal_score: 70\n", encoding="utf-8")
    config = AppConfig.load(path)
    assert config.poll_interval_minutes == 5
    assert config.notify.min_deal_score == 70


def test_watchlist_drives_query_specs(tmp_path):
    (tmp_path / "watchlist.yaml").write_text(
        "categories: [tools]\n"
        "items:\n"
        "  - name: Lathe\n"
        "    queries: [svarv, metallsvarv]\n"
        "    max_price: 5000\n",
        encoding="utf-8",
    )
    path = tmp_path / "config.yaml"
    path.write_text("search:\n  min_price: 100\n  max_price: 15000\n", encoding="utf-8")

    config = AppConfig.load(path)

    assert config.watchlist.categories == ["tools"]
    assert config.effective_categories() == ["tools"]
    specs = config.query_specs()
    assert [s.text for s in specs] == ["svarv", "metallsvarv"]
    assert all(s.max_price == 5000 for s in specs)  # per-item override
    assert all(s.min_price == 100 for s in specs)  # global fallback
    assert all(s.watch_item == "Lathe" for s in specs)  # tagged by origin item


def test_query_specs_fall_back_to_search_queries(tmp_path):
    # No watchlist.yaml next to the config, so the legacy search.queries win.
    path = tmp_path / "config.yaml"
    path.write_text(
        "search:\n  queries: [drill]\n  min_price: 50\n  max_price: 900\n", encoding="utf-8"
    )

    config = AppConfig.load(path)
    specs = config.query_specs()

    assert [s.text for s in specs] == ["drill"]
    assert specs[0].min_price == 50
    assert specs[0].max_price == 900


def test_inventory_loads_from_sibling_file(tmp_path):
    (tmp_path / "inventory.yaml").write_text(
        "items:\n"
        "  - name: Sieg C6 lathe\n"
        "    kind: lathe\n"
        "    specs:\n"
        "      spindle_taper: MT3\n"
        "      tailstock_taper: MT2\n"
        "    compatible: [ER32 collet chuck MT3]\n"
        "    duplicate_keywords: [Sieg C6, SC6]\n",
        encoding="utf-8",
    )
    path = tmp_path / "config.yaml"
    path.write_text("dry_run: true\n", encoding="utf-8")

    config = AppConfig.load(path)

    assert [i.name for i in config.inventory.items] == ["Sieg C6 lathe"]
    item = config.inventory.items[0]
    assert item.specs["spindle_taper"] == "MT3"
    assert item.compatible == ["ER32 collet chuck MT3"]
    assert "SC6" in item.duplicate_keywords


def test_load_retains_resolved_watchlist_path(tmp_path):
    (tmp_path / "watchlist.yaml").write_text("items: []\n", encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text("dry_run: true\n", encoding="utf-8")

    config = AppConfig.load(path)

    assert config.watchlist_path == str(tmp_path / "watchlist.yaml")


def test_watchlist_save_roundtrips(tmp_path):
    from shopper.config import WatchItem, Watchlist

    wl = Watchlist(
        categories=["tools"],
        items=[WatchItem(name="Lathe", queries=["svarv"], max_price=5000, priority="high")],
    )
    dest = tmp_path / "watchlist.yaml"
    wl.save(dest)

    reloaded = Watchlist.load(dest)
    assert reloaded.categories == ["tools"]
    assert [i.name for i in reloaded.items] == ["Lathe"]
    assert reloaded.items[0].max_price == 5000
    assert reloaded.items[0].priority == "high"
    # Defaults are omitted to keep the file terse.
    assert "min_price" not in dest.read_text(encoding="utf-8")
