"""Meta tracker — the agent learns the CURRENT meta from what's actually running.

A huge edge in memecoins is knowing the live meta: which lores, animals, and themes are
hot right now. When a fresh coin spawns riffing on a running meta (another toad while toads
are pumping, a lion while a lion is at 1.3M), that's a real signal it could work.

This periodically pulls the top runners, keeps a rolling cache, and asks Claude to distill
the current metas into a short list. That list is injected into every launch decision, so
the agent recognizes 'this new coin is in a hot meta' on its own.
"""

from __future__ import annotations

import time

from ..ingestion.scanner import scanner
from .llm import llm

_SYSTEM = """You track the live Solana memecoin meta. Given the coins running right now,
identify the CURRENT hot metas: the animals, lores, characters, and themes that are
pumping. Be concrete and current, not generic. This helps spot when a fresh launch is
riffing on something already working.

Respond ONLY as JSON:
{
  "metas": ["short punchy meta tags, e.g. 'toad/frog animals', 'lion coins', 'AI agents'"],
  "summary": "1-2 sentences on what's hot right now and why it matters for new launches"
}"""


class MetaTracker:
    def __init__(self) -> None:
        self.metas: list[str] = []
        self.summary: str = ""
        self.updated: float = 0.0
        self._recent_runners: list[dict] = []

    def context_for_brain(self) -> str:
        """One-liner injected into launch decisions."""
        if not self.metas:
            return ""
        return f"CURRENT HOT META (coins running now): {', '.join(self.metas[:8])}. {self.summary}"

    async def refresh(self) -> None:
        runners = await scanner.top_runners(limit=25)
        if not runners:
            return
        # Keep a rolling memory of runners we've seen (last ~60), dedup by symbol.
        seen = {r["symbol"] for r in self._recent_runners}
        for r in runners:
            if r["symbol"] not in seen:
                self._recent_runners.append(r)
        self._recent_runners = self._recent_runners[-60:]

        # Only the ones actually up meaningfully inform the "hot" read.
        hot = [r for r in self._recent_runners if r.get("change_h24", 0) and r["change_h24"] > 20]
        pool = hot or self._recent_runners
        listing = "\n".join(
            f"- {r['name']} ({r['symbol']}) mcap ${r['mcap']:,.0f} 24h {r.get('change_h24',0):+.0f}%"
            for r in pool[-30:]
        )

        if llm.available:
            data = await llm.json(_SYSTEM, f"Coins running now:\n{listing}\n\nWhat's the meta?",
                                   smart=False, max_tokens=300)
            if data and isinstance(data.get("metas"), list):
                self.metas = [str(m)[:40] for m in data["metas"]][:10]
                self.summary = str(data.get("summary", ""))[:300]
                self.updated = time.time()
        else:
            # Heuristic: most common keywords in running coin names.
            from collections import Counter
            words = Counter()
            for r in pool:
                for w in (r["name"] + " " + r["symbol"]).lower().split():
                    if len(w) > 2:
                        words[w] += 1
            self.metas = [w for w, _ in words.most_common(8)]
            self.summary = "trending keywords from current runners"
            self.updated = time.time()


meta_tracker = MetaTracker()
