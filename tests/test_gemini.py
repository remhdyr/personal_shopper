"""Tests for the shared prompt builder and Gemini provider (no network)."""

from __future__ import annotations

from shopper.ai.base import PromptBuilder, _inventory_brief
from shopper.ai.gemini import GeminiAnalyzer
from shopper.config import (
    Inventory,
    InventoryItem,
    LogisticsConfig,
    SearchConfig,
    WatchItem,
    Watchlist,
)
from shopper.models import Listing, Source
from shopper.preferences import PreferenceContext


def _listing() -> Listing:
    return Listing(
        source=Source.EBAY,
        source_id="1",
        title="MT3 ER32 collet chuck",
        url="https://example.com/1",
    )


def _prompts(**kwargs) -> PromptBuilder:
    return PromptBuilder(
        search=SearchConfig(categories=["lathe tooling"]),
        logistics=LogisticsConfig(),
        **kwargs,
    )


def _analyzer(**kwargs) -> GeminiAnalyzer:
    return GeminiAnalyzer(api_key="", model="test-model", prompts=_prompts(**kwargs))


def test_inventory_brief_renders_specs_accessories_and_compatible():
    inv = Inventory(
        items=[
            InventoryItem(
                name="Sieg C6 lathe",
                specs={"spindle_taper": "MT3", "tailstock_taper": "MT2"},
                accessories=["250-100 quick-change tool post"],
                compatible=["ER32 collet chuck MT3"],
            )
        ]
    )
    brief = _inventory_brief(inv)
    assert "Sieg C6 lathe" in brief
    assert "spindle_taper MT3" in brief
    assert "already own" in brief
    assert "250-100 quick-change tool post" in brief
    assert "ER32 collet chuck MT3" in brief


def test_prompt_includes_owned_gear_and_compatible_tooling():
    inv = Inventory(
        items=[
            InventoryItem(
                name="Sieg C6 lathe",
                specs={"spindle_taper": "MT3"},
                compatible=["ER32 collet chuck MT3"],
                duplicate_keywords=["Sieg C6"],
            )
        ]
    )
    prompt = _prompts(inventory=inv).build_prompt(_listing(), PreferenceContext())
    assert "ALREADY OWNS" in prompt
    assert "Sieg C6 lathe" in prompt
    assert "ER32 collet chuck MT3" in prompt


def test_prompt_includes_watchlist_when_present():
    wl = Watchlist(items=[WatchItem(name="Lathe", keywords=["Myford"], notes="prefer quality")])
    prompt = _prompts(watchlist=wl).build_prompt(_listing(), PreferenceContext())
    assert "watchlist" in prompt.lower()
    assert "Myford" in prompt


def test_prompt_omits_inventory_section_when_empty():
    prompt = _prompts().build_prompt(_listing(), PreferenceContext())
    assert "ALREADY OWNS" not in prompt


def test_parse_analysis_stamps_model():
    raw = '{"is_relevant": true, "deal_score": 80, "fit_score": 70, "summary": "ok"}'
    analysis = PromptBuilder.parse_analysis(raw, "test-model")
    assert analysis.model == "test-model"
    assert analysis.deal_score == 80


def test_fallback_is_non_relevant_and_stamped():
    analysis = PromptBuilder.fallback("test-model")
    assert analysis.is_relevant is False
    assert analysis.deal_score == 0
    assert analysis.model == "test-model"


async def test_analyze_stamps_model_on_verdict(monkeypatch):
    analyzer = _analyzer()

    def fake_generate(contents):
        return '{"is_relevant": true, "deal_score": 90, "fit_score": 60, "summary": "great"}'

    monkeypatch.setattr(analyzer, "_generate", fake_generate)

    analysis = await analyzer.analyze(_listing(), PreferenceContext())

    assert analysis.model == "test-model"
    assert analysis.deal_score == 90


async def test_suggest_watch_item_parses_ai_json(monkeypatch):
    analyzer = _analyzer()
    captured = {}

    def fake_generate(contents, *, schema, system):
        captured["schema"] = schema
        captured["system"] = system
        return (
            '{"name": "Metal lathe", "queries": ["metallsvarv", "mini lathe"], '
            '"keywords": ["Myford"], "notes": "prefer complete", "max_price": 5000, '
            '"priority": "high"}'
        )

    monkeypatch.setattr(analyzer, "_generate", fake_generate)

    draft = await analyzer.suggest_watch_item("a small metal lathe under 5000 kr")

    assert draft["name"] == "Metal lathe"
    assert draft["queries"] == ["metallsvarv", "mini lathe"]
    assert draft["max_price"] == 5000
    # It used the dedicated suggest schema/system, not the analysis one.
    assert "min_price" in captured["schema"]["properties"]
    assert "watch item" in captured["system"].lower()


def test_set_watchlist_swaps_guidance():
    prompts = _prompts()
    analyzer = GeminiAnalyzer(api_key="", model="test-model", prompts=prompts)
    wl = Watchlist(items=[WatchItem(name="Lathe", queries=["svarv"])])
    analyzer.set_watchlist(wl)
    prompt = prompts.build_prompt(_listing(), PreferenceContext())
    assert "Lathe" in prompt

