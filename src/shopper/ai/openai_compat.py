"""Analyzer for any OpenAI-compatible chat-completions endpoint.

Lets the load balancer borrow free/cheap bandwidth from providers that speak
the OpenAI ``/chat/completions`` shape (Qwen/DashScope, DeepSeek, local
vLLM, ...). The prompt, schema and reply parsing are shared with every other
provider via :class:`~shopper.ai.base.PromptBuilder`; this module only owns the
HTTP transport and the OpenAI request/response envelope.
"""

from __future__ import annotations

import base64
import json

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..logging_setup import get_logger
from ..models import DealAnalysis, Listing
from ..preferences import PreferenceContext
from .base import SUGGEST_SYSTEM, SYSTEM, PromptBuilder

log = get_logger(__name__)


class OpenAICompatAnalyzer:
    """Talk to an OpenAI-style ``/chat/completions`` endpoint.

    Vision inputs are passed as a base64 ``data:`` URL in the OpenAI
    ``image_url`` content-part shape, and JSON output is requested with
    ``response_format={"type": "json_object"}``. The schema itself lives in the
    prompt text (shared) since not every endpoint enforces a JSON schema.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        prompts: PromptBuilder,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._prompts = prompts
        self._http = httpx.AsyncClient(timeout=60, follow_redirects=True)

    @property
    def model_label(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._prompts.aclose()

    def set_watchlist(self, watchlist) -> None:
        self._prompts.set_watchlist(watchlist)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    async def _generate(self, messages: list[dict], *, system: str = SYSTEM) -> str:
        if not self._api_key:
            raise RuntimeError(f"API key not configured for {self._model}")
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        resp = await self._http.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"] or "{}"

    async def suggest_watch_item(self, text: str) -> dict:
        prompt = "Turn this buyer request into a watch item:\n\n" + text.strip()
        messages = [{"role": "user", "content": prompt}]
        raw = await self._generate(messages, system=SUGGEST_SYSTEM)
        return json.loads(raw)

    async def analyze(
        self,
        listing: Listing,
        context: PreferenceContext | None = None,
    ) -> DealAnalysis:
        prompt = self._prompts.build_prompt(listing, context or PreferenceContext())
        content: list[dict] = [{"type": "text", "text": prompt}]
        if listing.image_urls:
            if image := await self._prompts.fetch_image(listing.image_urls[0]):
                raw_bytes, mime = image
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    }
                )

        messages = [{"role": "user", "content": content}]
        try:
            raw = await self._generate(messages)
            return self._prompts.parse_analysis(raw, self._model)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully on AI failure
            log.error("%s analysis failed for %s: %s", self._model, listing.uid, exc)
            return self._prompts.fallback(self._model)
