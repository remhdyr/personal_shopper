"""Gemini-based listing analysis.

Sends a listing's text plus its first image to Gemini and asks for a structured
verdict: is it relevant, what's it worth, how good is the price (``deal_score``)
and how well does it match the user's taste (``fit_score``). The taste signal
comes from recent liked/disliked examples supplied as few-shot context, which is
how the service gets better over time without training a model.

The prompt, schema and reply parsing are shared across every provider via
:class:`~shopper.ai.base.PromptBuilder`; this module only owns the google-genai
transport.
"""

from __future__ import annotations

import asyncio
import json

from google import genai
from google.genai import types
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..logging_setup import get_logger
from ..models import DealAnalysis, Listing
from ..preferences import PreferenceContext
from .base import (
    RESPONSE_SCHEMA,
    SUGGEST_SCHEMA,
    SUGGEST_SYSTEM,
    SYSTEM,
    PromptBuilder,
)

log = get_logger(__name__)


class GeminiAnalyzer:
    def __init__(self, api_key: str, model: str, prompts: PromptBuilder) -> None:
        self._api_key = api_key
        self._model = model
        self._prompts = prompts
        self._client: genai.Client | None = None

    @property
    def model_label(self) -> str:
        return self._model

    def _ensure_client(self) -> genai.Client | None:
        if not self._api_key:
            return None
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def aclose(self) -> None:
        await self._prompts.aclose()

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _generate(
        self,
        contents: list,
        *,
        schema: dict = RESPONSE_SCHEMA,
        system: str = SYSTEM,
    ) -> str:
        client = self._ensure_client()
        if client is None:
            raise RuntimeError("Gemini API key not configured")
        response = client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.2,
            ),
        )
        return response.text or "{}"

    def set_watchlist(self, watchlist) -> None:
        """Point the analyzer at an updated wish list (after a dashboard edit)."""

        self._prompts.set_watchlist(watchlist)

    async def suggest_watch_item(self, text: str) -> dict:
        """Draft a structured watch item from a free-text request.

        Returns a plain dict matching the WatchItem shape for the buyer to
        review and edit before saving; raises on AI failure so the dashboard can
        surface the error instead of silently saving junk.
        """

        prompt = "Turn this buyer request into a watch item:\n\n" + text.strip()
        raw = await asyncio.to_thread(
            self._generate, [prompt], schema=SUGGEST_SCHEMA, system=SUGGEST_SYSTEM
        )
        return json.loads(raw)

    async def analyze(
        self,
        listing: Listing,
        context: PreferenceContext | None = None,
    ) -> DealAnalysis:
        prompt = self._prompts.build_prompt(listing, context or PreferenceContext())
        contents: list = [prompt]
        if listing.image_urls:
            if image := await self._prompts.fetch_image(listing.image_urls[0]):
                data, mime = image
                contents.append(types.Part.from_bytes(data=data, mime_type=mime))

        try:
            raw = await asyncio.to_thread(self._generate, contents)
            return self._prompts.parse_analysis(raw, self._model)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully on AI failure
            log.error("Gemini analysis failed for %s: %s", listing.uid, exc)
            return self._prompts.fallback(self._model)

