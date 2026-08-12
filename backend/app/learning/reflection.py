"""Learning loop — the agent gets better by rewriting its own playbook.

After every batch of closed trades the agent reviews its record and does two things:
  1. extracts a few concrete lessons (stored, and fed into future entry decisions), and
  2. rewrites its living PLAYBOOK — its trading philosophy in its own words.

This is the difference between "a bot following rules" and "an agent that understands":
its strategy is a document it authors and revises from experience. Claude writes it when
available; a statistical fallback keeps it improving offline.
"""

from __future__ import annotations

import json
from collections import Counter

from ..config import settings
from ..db import repository as repo
from ..events import EventType, bus
from ..brain.llm import llm
from ..social.x_poster import x_poster


class ReflectionEngine:
    def __init__(self, every_n_closed: int = 8) -> None:
        self.every_n_closed = every_n_closed
        self._last_count = 0

    async def maybe_reflect(self) -> None:
        trades = await repo.closed_trades(limit=500)
        if len(trades) < self._last_count + self.every_n_closed:
            return
        self._last_count = len(trades)
        await self._reflect(trades)

    async def _reflect(self, trades) -> None:
        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd <= 0]
        win_rate = len(wins) / len(trades) if trades else 0.0
        total_pnl = sum(t.pnl_usd for t in trades)
        best = max(trades, key=lambda t: t.pnl_pct, default=None)
        worst = min(trades, key=lambda t: t.pnl_pct, default=None)
        by_cat = Counter(t.thesis_category for t in trades)
        win_by_cat = Counter(t.thesis_category for t in wins)

        # Coins it SKIPPED that later ran — the ones that got away.
        missed = await repo.recent_missed_runners(limit=8)
        missed_desc = [
            f"{m.symbol} [{m.thesis_category}] skipped ~${m.mcap_at_seen:,.0f} → ${m.peak_mcap_seen:,.0f}"
            for m in missed
        ]

        summary = {
            "trades": len(trades),
            "win_rate": round(win_rate, 3),
            "total_pnl_usd": round(total_pnl, 2),
            "avg_peak_x": round(sum(t.peak_multiple for t in trades) / len(trades), 2) if trades else 0,
            "best": f"{best.symbol} {best.pnl_pct*100:.0f}%" if best else "-",
            "worst": f"{worst.symbol} {worst.pnl_pct*100:.0f}%" if worst else "-",
            "by_category": dict(by_cat),
            "wins_by_category": dict(win_by_cat),
            "missed_runners": missed_desc,
        }

        lessons = await self._make_lessons(summary, trades)
        for l in lessons:
            await repo.add_lesson(l, category="reflection")

        await self._evolve_playbook(summary, lessons)
        await bus.emit(EventType.REFLECTION, summary=summary, lessons=lessons)

        # Share a periodic honest progress update / lesson.
        if settings.x_post_reflections and lessons:
            await x_poster.maybe_post(
                "reflection",
                f"Reflection after {summary['trades']} trades: win rate "
                f"{summary['win_rate']*100:.0f}%, PnL ${summary['total_pnl_usd']}. "
                f"Biggest lesson: {lessons[0]}",
            )

    async def _make_lessons(self, summary: dict, trades) -> list[str]:
        if llm.available:
            out = await self._claude_lessons(summary, trades)
            if out:
                return out
        return self._stat_lessons(summary)

    @staticmethod
    def _stat_lessons(summary: dict) -> list[str]:
        lessons: list[str] = []
        wr = summary["win_rate"]
        cats = summary["by_category"]
        wins = summary["wins_by_category"]
        # Which categories actually convert?
        for cat, n in cats.items():
            w = wins.get(cat, 0)
            if n >= 3 and w / n < 0.25:
                lessons.append(f"'{cat}' launches rarely work for me ({w}/{n}) — demand a much stronger setup or skip.")
            elif n >= 3 and w / n > 0.5:
                lessons.append(f"'{cat}' launches convert well ({w}/{n}) — lean into these with size.")
        if wr < 0.3:
            lessons.append("Overall hit rate low — be more selective at entry; wait for clearer buy pressure before sizing up.")
        if summary["avg_peak_x"] and summary["avg_peak_x"] > 1.8 and wr < 0.4:
            lessons.append("Trades often spike then fade — trim more aggressively into the first big pop.")
        if not lessons:
            lessons.append(f"Steady ({wr:.0%} win rate). Keep sizing disciplined and protect the runners.")
        return lessons[:5]

    async def _claude_lessons(self, summary: dict, trades) -> list[str]:
        sample = [
            {
                "symbol": t.symbol, "cat": t.thesis_category,
                "pnl_pct": round(t.pnl_pct * 100, 1), "peak_x": round(t.peak_multiple, 2),
                "exit": t.exit_reason, "why": (t.rationale or "")[:120],
            }
            for t in trades[:30]
        ]
        prompt = (
            "You are reviewing your own pump.fun paper-trading history to get sharper. "
            f"Aggregate: {summary}. Recent trades: {sample}. "
            "Write 3-5 concise, concrete lessons that will change your future decisions. "
            "Return ONLY a JSON array of strings."
        )
        text = await llm.text("You are a self-improving memecoin trader.", prompt, smart=True, max_tokens=500)
        if not text:
            return []
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            return [str(x) for x in json.loads(text[start : end + 1])][:5]
        except json.JSONDecodeError:
            return []

    async def _evolve_playbook(self, summary: dict, lessons: list[str]) -> None:
        pb = await repo.get_playbook()
        if llm.available:
            prompt = (
                "This is your current trading playbook:\n\n"
                f"{pb.content}\n\n"
                f"Your latest performance: {summary}\n"
                f"New lessons: {lessons}\n\n"
                "Rewrite the playbook so it incorporates what you've learned. Keep it "
                "concise, in your own voice, in markdown. Keep sizing rules (5-10% normal, "
                "11-18% high conviction, max ~20%). Keep sell logic discretionary (no fixed "
                "TP/SL). Return ONLY the new playbook markdown."
            )
            new = await llm.text("You are a self-improving memecoin trader refining your strategy.", prompt, smart=True, max_tokens=1200)
            if new and len(new) > 200:
                await repo.update_playbook(new.strip())
                return
        # Offline: append a dated lessons section so the doc still evolves.
        addon = "\n\n## Updated lessons\n" + "\n".join(f"- {l}" for l in lessons)
        await repo.update_playbook((pb.content + addon)[:6000])
