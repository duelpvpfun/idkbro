"""Understanding layer — "what IS this coin?"

Mimics pump.fun's built-in AI summary: it takes the token's name, ticker, description
and any linked site/socials and produces a Thesis — a plain-language summary, a category,
and scores for narrative strength + virality. This is what lets the agent reason about a
coin like a human ("oh, this is the Nth dog coin today, weak" vs "this is a genuine
culture-moment play").

Claude does the reasoning when a key is present; otherwise a keyword heuristic fills in
so the agent still forms a usable thesis offline.
"""

from __future__ import annotations

import re

import httpx

from ..domain import Observation, Thesis
from .llm import llm

_CATEGORIES = ["animal", "meme", "political", "tech", "celebrity", "culture", "cashgrab"]

_SYSTEM = """You are the research brain of an autonomous memecoin trader. Given a freshly
launched pump.fun token, explain in plain language WHAT it is and judge its meme/narrative
potential. Be skeptical — most are low-effort cashgrabs. Respond ONLY with JSON:
{
  "category": one of ["animal","meme","political","tech","celebrity","culture","cashgrab","unknown"],
  "summary": "1-2 sentences: what is this coin, in plain English",
  "narrative_strength": 0.0-1.0,   // is the idea sticky / timely / ownable?
  "virality": 0.0-1.0,             // could this actually spread as a meme?
  "red_flags": ["short", "strings"]
}"""


class UnderstandingEngine:
    async def understand(self, obs: Observation) -> Thesis:
        launch = obs.launch
        # 1. Pull the off-chain metadata (description + socials) if not already loaded.
        await self._enrich_launch(launch)

        # 2. Optionally read the linked website for extra context.
        site_text = ""
        if launch.website:
            site_text = await self._fetch(launch.website)

        if llm.available:
            thesis = await self._claude(obs, site_text)
            if thesis is not None:
                return thesis
        return self._heuristic(obs, site_text)

    async def understand_coin(self, coin) -> Thesis:
        """Form a thesis for an existing (old) coin from the market scanner."""
        site_text = ""
        if coin.website:
            site_text = await self._fetch(coin.website)
        if llm.available:
            user = (
                f"NAME: {coin.name}\nTICKER: {coin.symbol}\n"
                f"DESCRIPTION: {coin.description or '(none)'}\n"
                f"LINKS: twitter={coin.twitter or '-'} website={coin.website or '-'}\n"
                f"SITE_TEXT: {site_text[:1500] or '(none)'}\n"
                f"CONTEXT: this is an EXISTING coin ~{coin.age_hours:.0f}h old showing renewed "
                f"activity (1h {coin.change_h1:+.0f}%, mcap ${coin.market_cap_usd:,.0f})."
            )
            data = await llm.json(_SYSTEM, user, smart=False, max_tokens=400)
            if data:
                cat = str(data.get("category", "unknown"))
                return Thesis(
                    category=cat if cat in _CATEGORIES or cat == "unknown" else "unknown",
                    summary=str(data.get("summary", ""))[:400],
                    narrative_strength=_clamp(data.get("narrative_strength", 0.5)),
                    virality=_clamp(data.get("virality", 0.5)),
                    red_flags=[str(x) for x in data.get("red_flags", [])][:5],
                )
        # Heuristic fallback using the coin's own text.
        blob = f"{coin.name} {coin.symbol} {coin.description} {site_text}".lower()
        category = "unknown"
        for c, words in {
            "animal": ["dog", "cat", "inu", "frog", "pepe", "shib", "floki"],
            "political": ["trump", "biden", "election", "maga", "president"],
            "tech": ["ai", "agent", "depin", "gpu", "compute", "protocol"],
            "celebrity": ["elon", "musk", "kanye", "celeb"],
        }.items():
            if any(w in blob for w in words):
                category = c
                break
        has_ctx = bool(coin.description or site_text or coin.twitter)
        return Thesis(
            category=category,
            summary=f"{coin.name} ({coin.symbol}): "
            + (coin.description[:160] if coin.description else "existing coin, limited public info."),
            narrative_strength=round(0.35 + (0.2 if has_ctx else 0), 3),
            virality=round(0.3 + (0.15 if has_ctx else 0), 3),
            red_flags=[] if has_ctx else ["thin public info"],
        )

    @staticmethod
    async def _enrich_launch(launch) -> None:
        """Populate description/socials from the pump.fun IPFS metadata JSON."""
        if launch.metadata_loaded or not launch.metadata_uri:
            return
        from ..ingestion.feed import load_metadata

        meta = await load_metadata(launch.metadata_uri)
        launch.metadata_loaded = True
        if not meta:
            return
        launch.description = launch.description or str(meta.get("description", "") or "")
        launch.twitter = launch.twitter or meta.get("twitter") or meta.get("twitter_url")
        launch.website = launch.website or meta.get("website")
        launch.telegram = launch.telegram or meta.get("telegram")
        if not launch.image:
            launch.image = meta.get("image")

    async def _claude(self, obs: Observation, site_text: str) -> Thesis | None:
        launch = obs.launch
        user = (
            f"NAME: {launch.name}\nTICKER: {launch.symbol}\n"
            f"DESCRIPTION: {launch.description or '(none)'}\n"
            f"LINKS: twitter={launch.twitter or '-'} website={launch.website or '-'} "
            f"telegram={launch.telegram or '-'}\n"
            f"SITE_TEXT: {site_text[:1500] or '(none)'}\n"
            f"EARLY_TAPE: {obs.unique_buyers} buyers, buys={obs.buys} sells={obs.sells}, "
            f"price {obs.price_change_pct:+.0f}% in first {obs.window_seconds:.0f}s"
        )
        data = await llm.json(_SYSTEM, user, smart=False, max_tokens=400)
        if not data:
            return None
        cat = str(data.get("category", "unknown"))
        return Thesis(
            category=cat if cat in _CATEGORIES or cat == "unknown" else "unknown",
            summary=str(data.get("summary", ""))[:400],
            narrative_strength=_clamp(data.get("narrative_strength", 0.5)),
            virality=_clamp(data.get("virality", 0.5)),
            red_flags=[str(x) for x in data.get("red_flags", [])][:5],
        )

    def _heuristic(self, obs: Observation, site_text: str) -> Thesis:
        launch = obs.launch
        blob = f"{launch.name} {launch.symbol} {launch.description} {site_text}".lower()
        category = "unknown"
        for cat, words in {
            "animal": ["dog", "cat", "inu", "frog", "pepe", "shib", "floki"],
            "political": ["trump", "biden", "election", "maga", "president"],
            "tech": ["ai", "agent", "depin", "gpu", "compute", "protocol"],
            "celebrity": ["elon", "musk", "kanye", "celeb"],
        }.items():
            if any(w in blob for w in words):
                category = cat
                break

        has_desc = bool(launch.description) or bool(site_text)
        has_socials = bool(launch.twitter or launch.website)
        red = []
        if not has_desc:
            red.append("no description / effort")
        if not has_socials:
            red.append("no socials")

        narrative = 0.2 + (0.25 if has_desc else 0) + (0.2 if has_socials else 0)
        if category in ("political", "tech", "celebrity"):
            narrative += 0.15
        virality = min(1.0, narrative + (0.15 if obs.unique_buyers > 15 else 0))
        if not has_desc and not has_socials:
            category = "cashgrab"

        summary = (
            f"{launch.name} ({launch.symbol}): "
            + (launch.description[:160] if launch.description else "low-effort launch, no stated concept.")
        )
        return Thesis(
            category=category,
            summary=summary,
            narrative_strength=round(min(1.0, narrative), 3),
            virality=round(virality, 3),
            red_flags=red,
        )

    @staticmethod
    async def _fetch(url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=6, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
                return re.sub(r"<[^>]+>", " ", r.text)[:4000]
        except (httpx.HTTPError, ValueError):
            return ""


def _clamp(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5
