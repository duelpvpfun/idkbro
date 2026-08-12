"""Thin Anthropic client wrapper with graceful degradation.

If no API key is set, `available` is False and callers fall back to their own heuristic
logic — so the whole agent still runs offline. Uses a cheap model for high-volume calls
(thesis, position checks) and a smarter model for entry conviction + reflection.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..config import settings


class LLM:
    def __init__(self) -> None:
        self._client = None
        self._day = int(time.time() // 86_400)
        self._used = 0
        if settings.has_llm:
            try:
                import anthropic

                self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            except ImportError:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _budget_ok(self) -> bool:
        today = int(time.time() // 86_400)
        if today != self._day:
            self._day = today
            self._used = 0
        return self._used < settings.llm_max_calls_per_day

    @property
    def used_today(self) -> int:
        today = int(time.time() // 86_400)
        if today != self._day:
            self._day = today
            self._used = 0
        return self._used

    async def json(
        self,
        system: str,
        user: str,
        *,
        smart: bool = False,
        max_tokens: int = 600,
    ) -> Optional[dict[str, Any]]:
        """Ask the model and parse a single JSON object from the reply."""
        if self._client is None or not self._budget_ok():
            return None
        model = settings.anthropic_smart_model if smart else settings.anthropic_fast_model
        try:
            self._used += 1
            resp = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            )
            return _parse_json(text)
        except Exception:
            return None

    async def text(self, system: str, user: str, *, smart: bool = True, max_tokens: int = 800):
        if self._client is None or not self._budget_ok():
            return None
        model = settings.anthropic_smart_model if smart else settings.anthropic_fast_model
        try:
            self._used += 1
            resp = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except Exception:
            return None

    async def describe_images(self, image_urls: list[str], instruction: str) -> str | None:
        """Vision: let the model actually look at images (e.g. KOL avatars) and describe
        their style, so the agent can draw inspiration from what it saw."""
        if self._client is None or not self._budget_ok() or not image_urls:
            return None
        content: list[dict] = [{"type": "text", "text": instruction}]
        for url in image_urls[:6]:
            content.append({"type": "image", "source": {"type": "url", "url": url}})
        try:
            self._used += 1
            resp = await self._client.messages.create(
                model=settings.anthropic_smart_model,
                max_tokens=500,
                messages=[{"role": "user", "content": content}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except Exception:
            return None


def _parse_json(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


llm = LLM()
