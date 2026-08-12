"""Revival detection — spotting an OLD coin waking back up.

Old pump.fun coins that died sometimes rip again (a KOL tweets it, a narrative returns, a
'community revival'). A human trader recognizes the pattern: dormant coin, then volume
suddenly spikes and price breaks upward, often reclaiming a level it dumped from.

Two stages:
  * RevivalDetector — cheap heuristic that flags candidates worth Claude's attention, so
    we don't burn LLM calls on every trending coin.
  * RevivalBrain — Claude judges whether it's a REAL revival (fresh catalyst, sustainable)
    or a dead-cat bounce / exit-liquidity trap, and returns conviction + size like entry.
"""

from __future__ import annotations

from ..config import settings
from ..domain import Action, EntryDecision, MarketCoin, RevivalSignal
from .llm import llm


class RevivalDetector:
    def detect(self, coin: MarketCoin) -> RevivalSignal:
        reasons: list[str] = []
        strength = 0.0
        pattern = ""

        # Must actually be an OLD coin, not a fresh launch (that's the other path).
        if coin.age_hours < settings.revival_min_age_hours:
            return RevivalSignal(waking=False, reasons=["too new — handled as a launch"])

        # Needs a real liquidity floor so it's tradeable and not a rug husk.
        if coin.liquidity_usd < settings.revival_min_liquidity_usd:
            return RevivalSignal(waking=False, reasons=["liquidity too thin"])

        # Volume acceleration: recent hour running hot vs the 24h average hour.
        avg_hour_vol = coin.vol_h24 / 24.0 if coin.vol_h24 else 0.0
        if avg_hour_vol > 0 and coin.vol_h1 > avg_hour_vol * settings.revival_vol_spike_mult:
            ratio = coin.vol_h1 / avg_hour_vol
            strength += min(0.4, 0.1 * ratio)
            reasons.append(f"volume {ratio:.1f}x its 24h-average hour")
            pattern = "volume_spike"

        # Price breaking upward now.
        if coin.change_h1 >= settings.revival_min_h1_change:
            strength += min(0.3, coin.change_h1 / 100.0)
            reasons.append(f"price +{coin.change_h1:.0f}% in 1h")
            pattern = pattern or "breakout"

        # Reclaim pattern: down over 24h but sharply up in the last hour (bouncing off lows).
        if coin.change_h24 < -20 and coin.change_h1 > 15:
            strength += 0.2
            reasons.append("reclaiming after a big drawdown")
            pattern = "reclaim"

        # Buyers outweigh sellers this hour.
        if coin.buys_h1 > coin.sells_h1 * 1.3 and coin.buys_h1 > 20:
            strength += 0.15
            reasons.append(f"buyers dominating ({coin.buys_h1} vs {coin.sells_h1})")

        strength = min(1.0, strength)
        waking = strength >= settings.revival_trigger_strength and bool(pattern)
        if not reasons:
            reasons.append("quiet — no revival signal")
        return RevivalSignal(waking=waking, strength=round(strength, 3), reasons=reasons, pattern=pattern)


_SYSTEM = """You are an autonomous memecoin trader evaluating whether an OLD pump.fun coin
is genuinely waking back up (worth buying) or just a dead-cat bounce / exit-liquidity trap.

Old coins CAN moon again — a returning narrative, a KOL, a community revival. But most
"pumps" on dead coins are traps where early bagholders dump on new buyers. Be skeptical
but opportunistic: if the revival looks real and sustainable, take it with sensible size.

You size by conviction: normal 0.05-0.10, high 0.11-0.18, never over 0.20.
Decide your OWN management plan. Respond ONLY with JSON:
{
  "action": "BUY" | "SKIP",
  "conviction": 0.0-1.0,
  "size_pct": 0.0-0.20,
  "thesis_tag": "short label",
  "rationale": "why this is / isn't a real revival",
  "plan": "how you'll manage it"
}"""


class RevivalBrain:
    async def decide(
        self, coin: MarketCoin, signal: RevivalSignal, thesis, playbook: str, lessons: list[str]
    ) -> EntryDecision:
        if llm.available:
            d = await self._claude(coin, signal, thesis, playbook, lessons)
            if d is not None:
                return d
        return self._heuristic(coin, signal, thesis)

    async def _claude(self, coin, signal, thesis, playbook, lessons) -> EntryDecision | None:
        user = (
            f"MY PLAYBOOK:\n{playbook[:1500]}\n\n"
            f"RECENT LESSONS:\n" + ("\n".join(f"- {x}" for x in lessons) or "- (none)")
            + "\n\n"
            f"OLD COIN REVIVAL CANDIDATE: {coin.name} ({coin.symbol})\n"
            f"age: {coin.age_hours:.1f}h | mcap: ${coin.market_cap_usd:,.0f} | "
            f"liq: ${coin.liquidity_usd:,.0f} | migrated_to_pumpswap: {coin.migrated}\n"
            f"price change: 5m {coin.change_m5:+.0f}% 1h {coin.change_h1:+.0f}% "
            f"6h {coin.change_h6:+.0f}% 24h {coin.change_h24:+.0f}%\n"
            f"volume: 1h ${coin.vol_h1:,.0f} vs 24h ${coin.vol_h24:,.0f} | "
            f"buys/sells 1h: {coin.buys_h1}/{coin.sells_h1}\n"
            f"REVIVAL SIGNAL: {signal.pattern} (strength {signal.strength:.2f}) — "
            f"{'; '.join(signal.reasons)}\n"
            f"WHAT IT IS: [{thesis.category}] {thesis.summary}\n\n"
            "Is this a real revival worth buying? Decide."
        )
        data = await llm.json(_SYSTEM, user, smart=True, max_tokens=450)
        if not data:
            return None
        action = Action.BUY if str(data.get("action", "")).upper() == "BUY" else Action.SKIP
        size = max(0.0, min(settings.size_hard_cap, float(data.get("size_pct", 0.0) or 0.0)))
        return EntryDecision(
            action=action,
            conviction=_clamp(data.get("conviction", 0.0)),
            size_pct=size if action == Action.BUY else 0.0,
            thesis_tag=str(data.get("thesis_tag", "revival"))[:60],
            rationale=str(data.get("rationale", ""))[:500],
            plan=str(data.get("plan", ""))[:400],
            tags=[thesis.category, "revival", "claude"],
        )

    def _heuristic(self, coin, signal, thesis) -> EntryDecision:
        score = 0.5 * signal.strength + 0.3 * thesis.narrative_strength + 0.2 * thesis.virality
        score = max(0.0, min(1.0, score))
        if score < 0.5:
            return EntryDecision(
                action=Action.SKIP, conviction=round(score, 3), size_pct=0.0,
                thesis_tag="revival",
                rationale=f"revival strength {signal.strength:.2f} not convincing enough → SKIP.",
                tags=[thesis.category, "revival", "rules"],
            )
        high = score >= 0.7
        size = (settings.size_high_min if high else settings.size_normal_min)
        return EntryDecision(
            action=Action.BUY, conviction=round(score, 3),
            size_pct=round(min(settings.size_hard_cap, size), 4), thesis_tag="revival",
            rationale=(
                f"old coin waking up ({signal.pattern}, strength {signal.strength:.2f}): "
                f"{'; '.join(signal.reasons[:2])}. Taking a position."
            ),
            plan="Revival play — momentum can be violent but brief. Trim fast into the spike, "
                 "cut if the bounce stalls; only hold if volume keeps building.",
            tags=[thesis.category, "revival", "rules"],
        )


def _clamp(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0
