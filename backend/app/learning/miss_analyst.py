"""Miss analyst — the agent learns from coins it skipped that later ran.

When a coin the agent passed on multiplies, it doesn't just note it: it stops and asks
itself WHY it missed it and what signal it should have caught. Claude compares the agent's
original skip reasoning against how the coin actually played out, forms a concrete
conjecture, and saves it as a lesson so the same miss is less likely next time. This lets
the agent improve on its own, without a human explaining every coin.
"""

from __future__ import annotations

from ..brain.llm import llm
from ..db import repository as repo
from ..events import EventType, bus

_SYSTEM = """You are a memecoin trader reviewing a coin you SKIPPED that then ran hard. Be
honest and specific. Figure out what you actually missed and form a concrete, transferable
conjecture so you catch this kind of setup next time. Do not be vague or write a generic
'do more research' bow. Name the specific signal or pattern.

Respond ONLY as JSON:
{
  "what_i_missed": "the specific thing you overlooked, first person, 1-2 sentences",
  "lesson": "a concrete rule/pattern to watch for next time, in your own words"
}
No em dash."""


class MissAnalyst:
    async def analyze(self, coin, watch) -> None:
        """coin = live MarketCoin, watch = WatchlistCoin record (has skip_reason)."""
        ran_x = (
            coin.market_cap_usd / watch.mcap_at_seen if watch.mcap_at_seen else 0
        )
        await bus.emit(
            EventType.THOUGHT,
            text=f"🤔 wait, i skipped {coin.symbol} and it ran {ran_x:.1f}x. why did i miss that?",
            symbol=coin.symbol, mint=coin.mint,
        )

        lesson_text = ""
        if llm.available:
            user = (
                f"COIN: {coin.name} ({coin.symbol})\n"
                f"WHEN I SAW IT: mcap ${watch.mcap_at_seen:,.0f}, "
                f"my thesis was '{watch.thesis or 'none'}'\n"
                f"WHY I SKIPPED IT: {watch.skip_reason or 'unknown'}\n"
                f"WHAT HAPPENED: it ran to ${coin.market_cap_usd:,.0f} ({ran_x:.1f}x). "
                f"Now: age {coin.age_hours:.0f}h, 24h {coin.change_h24:+.0f}%, "
                f"liq ${coin.liquidity_usd:,.0f}, twitter={coin.twitter or 'none'}.\n\n"
                "What did I miss and what should I watch for next time?"
            )
            data = await llm.json(_SYSTEM, user, smart=True, max_tokens=300)
            if data:
                missed = str(data.get("what_i_missed", "")).replace("—", ", ")
                lesson_text = str(data.get("lesson", "")).replace("—", ", ")
                if missed:
                    await bus.emit(EventType.THOUGHT, text=f"💡 {missed}", symbol=coin.symbol)

        if not lesson_text:
            lesson_text = (
                f"I skipped {coin.symbol} ({watch.thesis_category}) at "
                f"${watch.mcap_at_seen:,.0f} and it ran {ran_x:.1f}x. Reconsider setups like "
                f"this: {watch.skip_reason[:120]}"
            )

        # Save as a lesson (feeds future entry decisions) + as remembered context.
        await repo.add_lesson(f"(from a miss) {lesson_text}", category="miss")
        await repo.add_market_knowledge(
            f"Skipped {coin.symbol}, it ran {ran_x:.1f}x. Skip reason was: {watch.skip_reason}",
            topic=coin.symbol, takeaway=lesson_text, category="miss",
        )
        await repo.mark_postmortem_done(coin.mint)
        await bus.emit(
            EventType.REFLECTION,
            summary={"trades": 0, "win_rate": 0, "total_pnl_usd": 0, "avg_peak_x": ran_x},
            lessons=[f"missed {coin.symbol} ({ran_x:.1f}x): {lesson_text}"],
        )


miss_analyst = MissAnalyst()
