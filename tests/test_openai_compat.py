"""Tests for the OpenAI-compatible analyzer (no network)."""

from __future__ import annotations

import base64

import httpx

from shopper.ai.base import PromptBuilder
from shopper.ai.openai_compat import OpenAICompatAnalyzer
from shopper.config import LogisticsConfig, SearchConfig
from shopper.models import Listing, Source
from shopper.preferences import PreferenceContext


def _prompts() -> PromptBuilder:
    return PromptBuilder(
        search=SearchConfig(categories=["lathe tooling"]),
        logistics=LogisticsConfig(),
    )


def _analyzer(handler, image_handler=None) -> OpenAICompatAnalyzer:
    prompts = _prompts()
    prompts._http = httpx.AsyncClient(
        transport=httpx.MockTransport(image_handler or (lambda r: httpx.Response(404)))
    )
    analyzer = OpenAICompatAnalyzer(
        base_url="https://api.example.com/v1",
        model="qwen-vl-max",
        api_key="secret",
        prompts=prompts,
    )
    analyzer._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return analyzer


def _listing_with_image() -> Listing:
    return Listing(
        source=Source.EBAY,
        source_id="1",
        title="MT3 ER32 collet chuck",
        url="https://example.com/1",
        image_urls=["https://example.com/img.jpg"],
    )


async def test_analyze_sends_json_request_and_stamps_model():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read().decode()
        content = (
            '{"is_relevant": true, "deal_score": 85, "fit_score": 70, '
            '"summary": "solid"}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    analyzer = _analyzer(handler)
    analysis = await analyzer.analyze(
        Listing(source=Source.EBAY, source_id="1", title="x", url="https://e/1"),
        PreferenceContext(),
    )

    assert analysis.deal_score == 85
    assert analysis.model == "qwen-vl-max"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer secret"
    assert '"response_format"' in captured["body"]


async def test_analyze_includes_base64_image_data_url():
    captured = {}
    png = b"\x89PNG\r\n\x1a\nfakeimage"

    def image_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        content = '{"is_relevant": true, "deal_score": 50, "fit_score": 50, "summary": "ok"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    analyzer = _analyzer(handler, image_handler=image_handler)
    await analyzer.analyze(_listing_with_image(), PreferenceContext())

    expected = base64.b64encode(png).decode("ascii")
    assert "data:image/png;base64," + expected in captured["body"]
    assert '"image_url"' in captured["body"]


async def test_analyze_returns_fallback_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    analyzer = _analyzer(handler)
    analysis = await analyzer.analyze(
        Listing(source=Source.EBAY, source_id="1", title="x", url="https://e/1"),
        PreferenceContext(),
    )

    assert analysis.is_relevant is False
    assert analysis.summary == "analysis unavailable"
    assert analysis.model == "qwen-vl-max"


async def test_suggest_watch_item_parses_json():
    def handler(request: httpx.Request) -> httpx.Response:
        content = '{"name": "Metal lathe", "queries": ["metallsvarv"]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    analyzer = _analyzer(handler)
    draft = await analyzer.suggest_watch_item("a small metal lathe")

    assert draft["name"] == "Metal lathe"
    assert draft["queries"] == ["metallsvarv"]
