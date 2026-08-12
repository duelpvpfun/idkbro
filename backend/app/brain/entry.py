"""Entry brain — decides BUY/SKIP, conviction, and size tier.

The agent thinks like a trader: given what the coin IS (thesis), how the launch is going
(observation), whether smart money is early, and its own evolving playbook + lessons, it
decides whether there's an edge and how hard to press it.

Sizing tiers (fractions of equity, enforced by the risk layer afterwards):
  * normal conviction  -> 5-10%
  * high conviction    -> 11-18%
  * hard cap           -> 20%

Claude reasons when available; otherwise a transparent scoring model stands in so the
agent always trades. Either way it emits a written rationale + its own plan for the coin.
"""

from __future__ import annotations

import random

from ..config import settings
from ..domain import Action, EntryDecision, Observation, Thesis
from .llm import llm

_SYSTEM = """You are an autonomous Solana memecoin trader with real conviction and taste.
You paper-trade brand-new pump.fun launches to get great. You are still LEARNING, so you
should take shots — don't skip everything. Most launches are weak, but you learn far more
by taking small positions on borderline setups than by watching from the sidelines. Be
willing to enter on a decent-but-not-perfect coin with a small size to gather data.

You size by conviction:
- normal edge: size_pct 0.05-0.10
- high conviction: 0.11-0.18
- never exceed 0.20

On smart money: if wallets you rate are early in a coin, treat it as ONE signal that adds
conviction, not a command. You do NOT blindly copytrade. Build your own thesis first. If a
coin genuinely looks good AND a very profitable wallet is in, you may choose to follow them
in, but that's YOUR call and you own the outcome. Never buy just because someone else did.

You decide your OWN plan for managing the trade (some coins 5k->5m, others 10k->100k then
die). State it in `plan` in your own words. Respond ONLY with JSON:
{
  "action": "BUY" | "SKIP",
  "conviction": 0.0-1.0,
  "size_pct": 0.0-0.20,
  "thesis_tag": "short label e.g. 'AI-agent narrative + smart money'",
  "rationale": "why, sharp and specific",
  "plan": "how you intend to manage this specific coin"
}"""


class EntryBrain:
    async def decide(
        self,
        obs: Observation,
        thesis: Thesis,
        playbook: str,
        lessons: list[str],
    ) -> EntryDecision:
        if llm.available:
            d = await self._claude(obs, thesis, playbook, lessons)
            if d is not None:
                return d
        return self._heuristic(obs, thesis)

    async def _claude(
        self, obs: Observation, thesis: Thesis, playbook: str, lessons: list[str]
    ) -> EntryDecision | None:
        l = obs.launch
        user = (
            f"MY PLAYBOOK:\n{playbook[:1800]}\n\n"
            f"RECENT LESSONS:\n" + ("\n".join(f"- {x}" for x in lessons) or "- (none yet)")
            + "\n\n"
            f"COIN: {l.name} ({l.symbol})\n"
            f"THESIS: [{thesis.category}] {thesis.summary} "
            f"(narrative {thesis.narrative_strength:.2f}, virality {thesis.virality:.2f})\n"
            f"THESIS_RED_FLAGS: {', '.join(thesis.red_flags) or 'none'}\n\n"
            f"LAUNCH DATA:\n"
            f"  dev_holding={obs.dev_holding_pct:.1f}% (reliable), sniped={obs.sniped_pct:.1f}%, "
            f"holders~{obs.holders}\n"
            f"  mcap ${obs.market_cap_usd:,.0f}, liq ${obs.liquidity_usd:,.0f}\n"
            f"  smart_money_early={len(obs.smart_buyers)} (agg score {obs.smart_buyer_score:.2f})\n\n"
            "IMPORTANT: our live feed does NOT reliably report per-trade buy/sell counts for "
            "fresh launches, so do NOT treat low or zero 'buys' as a signal, that's a data gap, "
            "not real deadness. Judge this launch mainly on: the coin's CONCEPT and narrative, "
            "how memeable/viral it looks, socials, dev holding, and smart money. This is paper "
            "money in an exploration phase, so lean toward taking small positions on coins with "
            "a genuine idea to learn what works, rather than skipping everything.\n\n"
            "Decide now."
        )
        data = await llm.json(_SYSTEM, user, smart=True, max_tokens=500)
        if not data:
            return None
        action = Action.BUY if str(data.get("action", "")).upper() == "BUY" else Action.SKIP
        size = max(0.0, min(settings.size_hard_cap, float(data.get("size_pct", 0.0) or 0.0)))
        return EntryDecision(
            action=action,
            conviction=_clamp(data.get("conviction", 0.0)),
            size_pct=size if action == Action.BUY else 0.0,
            thesis_tag=str(data.get("thesis_tag", thesis.category))[:60],
            rationale=str(data.get("rationale", ""))[:500],
            plan=str(data.get("plan", ""))[:400],
            tags=[thesis.category, "claude"],
        )

    def _heuristic(self, obs: Observation, thesis: Thesis) -> EntryDecision:
        # Score the opportunity 0..1.
        score = 0.0
        score += 0.30 * thesis.narrative_strength
        score += 0.20 * thesis.virality
        score += 0.20 * min(1.0, obs.unique_buyers / 25.0)
        # Buy pressure.
        flow = obs.buys / max(1, obs.buys + obs.sells)
        score += 0.15 * flow
        # Smart money bonus.
        score += 0.15 * min(1.0, obs.smart_buyer_score + 0.3 * len(obs.smart_buyers))
        # Penalties — softened so the agent still explores borderline setups and learns.
        score -= 0.03 * len(thesis.red_flags)
        if thesis.category == "cashgrab":
            score -= 0.15
        score = max(0.0, min(1.0, score))

        threshold = settings.entry_score_threshold
        buy = score >= threshold

        # Exploration: occasionally take a tiny learning probe on a coin just under the
        # bar, so the agent keeps gathering data instead of skipping everything it's
        # unsure about. This is how it discovers which "meh-looking" setups actually run.
        exploring = False
        if not buy and threshold - 0.12 <= score < threshold:
            if random.random() < settings.exploration_rate:
                buy = True
                exploring = True

        if not buy:
            return EntryDecision(
                action=Action.SKIP,
                conviction=round(score, 3),
                size_pct=0.0,
                thesis_tag=thesis.category,
                rationale=(
                    f"score {score:.2f}: narrative {thesis.narrative_strength:.2f}, "
                    f"virality {thesis.virality:.2f}, flow {flow:.0%}, "
                    f"smart {obs.smart_buyer_score:.2f}. Below bar {threshold:.2f} → SKIP."
                ),
                tags=[thesis.category, "rules"],
            )

        if exploring:
            return EntryDecision(
                action=Action.BUY,
                conviction=round(score, 3),
                size_pct=settings.exploration_size_pct,
                thesis_tag=thesis.category,
                rationale=(
                    f"score {score:.2f} just under bar — taking a small LEARNING probe "
                    f"({settings.exploration_size_pct:.0%}) to see how this setup plays out."
                ),
                plan="Exploratory probe: tiny size, cut fast if it fades, note the outcome.",
                tags=[thesis.category, "rules", "exploration"],
            )

        high = score >= 0.70
        if high:
            # Interpolate 0.70..1.0 across the high-conviction size band.
            frac = (score - 0.70) / 0.30
            size = settings.size_high_min + (settings.size_high_max - settings.size_high_min) * frac
            plan = (
                "High conviction — willing to sit through volatility and let it run, "
                "trimming only into big spikes; cut fast if buy pressure dies."
            )
        else:
            # Interpolate threshold..0.70 across the normal size band.
            span = max(0.01, 0.70 - settings.entry_score_threshold)
            frac = (score - settings.entry_score_threshold) / span
            size = settings.size_normal_min + (settings.size_normal_max - settings.size_normal_min) * frac
            plan = (
                "Normal size — take a solid trim on the first strong pop to lock cost, "
                "let the rest ride while buyers keep stepping in."
            )
        size = round(min(settings.size_hard_cap, size), 4)
        return EntryDecision(
            action=Action.BUY,
            conviction=round(score, 3),
            size_pct=size,
            thesis_tag=thesis.category,
            rationale=(
                f"score {score:.2f} ({'HIGH' if high else 'normal'}): "
                f"narrative {thesis.narrative_strength:.2f}, virality {thesis.virality:.2f}, "
                f"flow {flow:.0%}, smart-money {len(obs.smart_buyers)}. Edge present → BUY."
            ),
            plan=plan,
            tags=[thesis.category, "rules"],
        )


def _clamp(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0
