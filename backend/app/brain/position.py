"""Position brain — the agent manages its own exits.

Per the user: DON'T tell it when to sell. Some coins go 5k->5m, others 10k->100k and die.
So this brain looks at the live state of each open position — current multiple, distance
from peak, recent buy/sell flow, time held, and its own entry plan — and decides HOLD,
TRIM (sell part), SELL (exit), or ADD.

Claude drives the judgment when available. The heuristic fallback is intentionally
non-mechanical: it reacts to momentum and drawdown-from-peak rather than fixed % targets,
so behavior still looks like discretionary trading rather than a rigid TP/SL bot.
"""

from __future__ import annotations

from ..domain import ManageAction, ManageDecision
from .llm import llm

_SYSTEM = """You are managing an OPEN memecoin position you entered. You have full
discretion over take-profit and when to cut — think like a sharp trader: let real runners
run, trim into parabolic spikes to bank profit, cut fast when buy pressure dies or the move
is clearly over. Consider your original plan for this coin.

Note: a hard survival stop-loss runs automatically underneath you, so you never have to
'hope' a deep loser recovers — you're free to cut a broken thesis early, but you don't need
to babysit catastrophic downside; that floor has you covered. Focus on maximizing good
exits, not on preventing rugs.

Respond ONLY with JSON:
{
  "action": "HOLD" | "TRIM" | "SELL" | "ADD",
  "trim_fraction": 0.0-1.0,   // only for TRIM: portion of remaining tokens to sell
  "reason": "concise justification"
}"""


class PositionBrain:
    async def manage(self, state: dict) -> ManageDecision:
        if llm.available:
            d = await self._claude(state)
            if d is not None:
                return d
        return self._heuristic(state)

    async def _claude(self, s: dict) -> ManageDecision | None:
        kind = s.get("source", "launch")
        kind_note = (
            "This is a REVIVAL play on an OLD coin — these moves are often fast and brief, "
            "so protect profit quickly and don't overstay."
            if kind == "revival"
            else "This is a fresh launch — it can still be early in its run."
        )
        user = (
            f"COIN: {s['symbol']} ({kind})\n{kind_note}\n"
            f"entry_mcap: ${s['entry_mcap']:,.0f} | now_mcap: ${s['now_mcap']:,.0f}\n"
            f"current_multiple: {s['multiple']:.2f}x | peak_multiple: {s['peak_multiple']:.2f}x\n"
            f"drawdown_from_peak: {s['dd_from_peak']:.0%}\n"
            f"held: {s['held_min']:.1f} min | tokens_remaining: {s['frac_remaining']:.0%} of entry\n"
            f"recent_flow: buys={s['recent_buys']} sells={s['recent_sells']} "
            f"(net {s['net_flow']:+d} last window)\n"
            f"unrealized_pnl: {s['unreal_pct']:+.0f}%\n"
            f"MY ORIGINAL PLAN: {s['plan'] or '(none)'}\n\n"
            "Decide what to do with this position now."
        )
        data = await llm.json(_SYSTEM, user, smart=False, max_tokens=300)
        if not data:
            return None
        raw = str(data.get("action", "HOLD")).upper()
        action = ManageAction.__members__.get(raw, ManageAction.HOLD)
        frac = 0.0
        if action == ManageAction.TRIM:
            try:
                frac = max(0.05, min(0.95, float(data.get("trim_fraction", 0.5))))
            except (TypeError, ValueError):
                frac = 0.5
        return ManageDecision(action=action, trim_fraction=frac, reason=str(data.get("reason", ""))[:300])

    def _heuristic(self, s: dict) -> ManageDecision:
        mult = s["multiple"]
        dd = s["dd_from_peak"]
        net = s["net_flow"]
        held = s["held_min"]
        peak = s["peak_multiple"]

        # Momentum died hard off a high -> protect gains / cut.
        if peak >= 1.4 and dd >= 0.35:
            if mult > 1.05:
                return ManageDecision(ManageAction.SELL, reason=f"gave back {dd:.0%} from peak {peak:.1f}x, banking gains")
            return ManageDecision(ManageAction.SELL, reason=f"rolled over off peak {peak:.1f}x, cutting")

        # Parabolic spike -> trim into strength.
        if mult >= 2.0 and net > 0:
            return ManageDecision(ManageAction.TRIM, trim_fraction=0.4, reason=f"{mult:.1f}x with buyers still in — trim 40% to de-risk")
        if mult >= 3.5:
            return ManageDecision(ManageAction.TRIM, trim_fraction=0.5, reason=f"{mult:.1f}x — take half off the table")

        # Thesis breaking: sustained selling while underwater.
        if net < -3 and mult < 0.9:
            return ManageDecision(ManageAction.SELL, reason="net selling and underwater — thesis not playing out")

        # Dead tape early: no follow-through.
        if held > 6 and abs(mult - 1.0) < 0.08 and s["recent_buys"] < 2:
            return ManageDecision(ManageAction.SELL, reason="no follow-through, capital better elsewhere")

        # Fresh, buyers stepping in, up modestly -> let it cook.
        return ManageDecision(ManageAction.HOLD, reason="thesis intact, letting it develop")
