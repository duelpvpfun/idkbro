"""Advice evaluator — the agent weighs your input like a smart trader weighs a mentor.

You give the agent insight ("dog coins with a real X account run harder", "avoid buying
after 30s", whatever). It does NOT blindly obey. It critically evaluates your advice
against its own playbook and track record, then decides a stance:

  * adopt   — it agrees and folds a concrete rule into its style
  * partial — it takes part of it, with caveats
  * reject  — it disagrees (and says why), keeps its own approach

Only 'adopt'/'partial' advice becomes active and gets fed into future entry decisions.
This keeps you in an advisory seat, not the driver's seat — exactly as requested.
"""

from __future__ import annotations

import re

from ..db import repository as repo
from ..events import EventType, bus
from .llm import llm

_SYSTEM = """You are an autonomous memecoin trader with your own hard-won style. A human
you trust is giving you advice. You respect them, but you do NOT blindly obey — you think
for yourself. Evaluate their advice critically against your own playbook and results, then
decide how much of it to actually adopt.

Be honest: if the advice is good, adopt it and say so. If it's partly useful, take the
useful part. If you disagree or it contradicts what your data shows, respectfully reject
it and explain why. You are the trader; they are an advisor.

Respond ONLY with JSON:
{
  "stance": "adopt" | "partial" | "reject",
  "reasoning": "your honest take, in first person, 1-3 sentences",
  "adopted_rule": "if adopt/partial: a concise rule in your own words to add to your style; else empty"
}"""


class AdviceEvaluator:
    async def evaluate(self, advice_id: int, text: str) -> None:
        playbook = (await repo.get_playbook()).content
        closed = await repo.closed_trades(limit=60)
        wins = sum(1 for t in closed if t.pnl_usd > 0)
        record = f"{wins}/{len(closed)} winners" if closed else "no closed trades yet"

        stance, reasoning, rule = await self._judge(text, playbook, record)

        await repo.set_advice_verdict(advice_id, stance, reasoning, rule)

        # If adopted, fold the rule into the living playbook so it shapes real behavior.
        if stance in ("adopt", "partial") and rule:
            await self._fold_into_playbook(playbook, rule, text)

        await bus.emit(
            EventType.ADVICE, advice=text, stance=stance, reasoning=reasoning,
            adopted_rule=rule,
        )

    async def _judge(self, text: str, playbook: str, record: str):
        if llm.available:
            user = (
                f"MY CURRENT PLAYBOOK:\n{playbook[:1600]}\n\n"
                f"MY RECENT RECORD: {record}\n\n"
                f"ADVICE FROM MY MENTOR:\n\"{text}\"\n\n"
                "Decide your stance."
            )
            data = await llm.json(_SYSTEM, user, smart=True, max_tokens=400)
            if data:
                stance = str(data.get("stance", "partial")).lower()
                if stance not in ("adopt", "partial", "reject"):
                    stance = "partial"
                return (
                    stance,
                    str(data.get("reasoning", ""))[:500],
                    str(data.get("adopted_rule", ""))[:300],
                )
        # Offline fallback: cautiously take advice as a partial note without over-trusting.
        return (
            "partial",
            "No LLM available to reason deeply, so I'll note this as a soft consideration "
            "rather than a hard rule until my own results confirm it.",
            f"Consider (unverified): {text[:200]}",
        )

    async def _fold_into_playbook(self, playbook: str, rule: str, original: str) -> None:
        if llm.available:
            prompt = (
                f"This is your trading playbook:\n\n{playbook}\n\n"
                f"You've decided to adopt this rule from your mentor's advice: \"{rule}\"\n"
                f"(original advice: \"{original}\")\n\n"
                "Integrate it naturally into the playbook in your own voice. Keep sizing "
                "rules (5-10% normal, 11-18% high conviction, max ~20%) and discretionary "
                "exits. Return ONLY the updated playbook markdown."
            )
            new = await llm.text(
                "You are refining your own trading playbook.", prompt, smart=True, max_tokens=1200
            )
            if new and len(new) > 200:
                await repo.update_playbook(new.strip())
                return
        # Offline: append the adopted rule under a mentor-advice section.
        addon = f"\n\n## Adopted from mentor\n- {rule}"
        await repo.update_playbook((playbook + addon)[:6000])


_KNOWLEDGE_SYSTEM = """Your mentor is explaining WHY a coin moved (context, not an order).
This is knowledge to remember, not a rule to obey. Distill the transferable PATTERN so you
can recognize a similar setup or narrative in the future. Don't change your strategy from
one story; just note what to watch for.

Respond ONLY as JSON:
{ "takeaway": "the pattern in your own words, 1-2 sentences, first person, no em dash" }"""


_CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


class KnowledgeProcessor:
    """Handles taught context ('why X ran'). Stored as memory the agent pattern-matches
    against later, NOT as a behavior rule. This is how the user shares understanding
    without the agent getting rigidly influenced.

    If the input contains a Solana contract address, the agent AUTONOMOUSLY pulls that
    coin's live data (name, mcap, chart, socials) so it grounds your insight in the real
    token instead of just a story."""

    async def process(self, note_id: int, text: str, topic: str) -> None:
        # If a CA is present, go fetch the coin ourselves and enrich the context.
        coin_ctx = ""
        ca = self._extract_ca(f"{topic} {text}")
        if ca:
            coin_ctx = await self._lookup_ca(ca)

        takeaway = text[:200]
        if llm.available:
            user = (
                f"COIN/TOPIC: {topic or 'unspecified'}\n"
                f"WHAT MY MENTOR EXPLAINED:\n\"{text}\"\n"
                + (f"\nLIVE DATA I PULLED ON THIS COIN:\n{coin_ctx}\n" if coin_ctx else "")
                + "\nDistill the transferable pattern."
            )
            data = await llm.json(_KNOWLEDGE_SYSTEM, user, smart=False, max_tokens=220)
            if data and data.get("takeaway"):
                takeaway = str(data["takeaway"]).replace("—", ", ")[:300]
        await repo.set_knowledge_takeaway(note_id, takeaway)
        await bus.emit(
            EventType.ADVICE, advice=f"[knowledge] {topic or text[:40]}",
            stance="noted", reasoning=(coin_ctx + " | " if coin_ctx else "") + takeaway,
            adopted_rule="",
        )

    async def study_ca(self, ca: str, note: str = "") -> dict:
        """You paste a CA. The agent checks its OWN history with the coin (did I see it? skip
        it? trade it?), pulls how it actually performed, and works out the lesson itself. If
        it skipped a coin that then ran, it reflects on what it missed. Scales better than the
        user explaining every coin: just fire CAs at it and it self-teaches."""
        coin_ctx = await self._lookup_ca(ca)
        watch = await repo.get_watch(ca)
        history = "I have no record of seeing this coin."
        if watch is not None:
            ran = (
                watch.peak_mcap_seen / watch.mcap_at_seen
                if watch.mcap_at_seen else 0
            )
            history = (
                f"I saw this coin. My disposition: {watch.disposition}. "
                f"When I saw it mcap was ${watch.mcap_at_seen:,.0f}, it later peaked around "
                f"${watch.peak_mcap_seen:,.0f} ({ran:.1f}x from when I saw it). "
                f"Thesis I had: {watch.thesis or 'none'}."
            )

        takeaway = ""
        if llm.available:
            user = (
                f"A CA was sent to me to study: {ca}\n"
                + (f"Note from my mentor: {note}\n" if note else "")
                + f"MY OWN HISTORY WITH IT: {history}\n"
                + (f"LIVE DATA NOW: {coin_ctx}\n" if coin_ctx else "")
                + "\nBe honest with yourself. If I skipped a coin that ran, what did I miss "
                "and what pattern should I watch for next time? If I caught it, what worked? "
                "Distill ONE transferable lesson in my own words."
            )
            data = await llm.json(_KNOWLEDGE_SYSTEM, user, smart=True, max_tokens=250)
            if data and data.get("takeaway"):
                takeaway = str(data["takeaway"]).replace("—", ", ")[:300]

        # Store it as remembered context.
        combined = f"{note} | {coin_ctx}".strip(" |")
        saved = await repo.add_market_knowledge(
            combined or f"studied {ca}", topic=ca[:12], takeaway=takeaway, category="ca_study"
        )
        await bus.emit(
            EventType.ADVICE, advice=f"[studied CA] {ca[:10]}...",
            stance="noted", reasoning=f"{history} {('| ' + takeaway) if takeaway else ''}"[:400],
            adopted_rule="",
        )
        return {"history": history, "live": coin_ctx, "takeaway": takeaway}

    @staticmethod
    def _extract_ca(text: str) -> str:
        """Find a plausible Solana mint address (base58, 32-44 chars). Prefer pump.fun ones."""
        candidates = _CA_RE.findall(text or "")
        if not candidates:
            return ""
        for c in candidates:
            if c.lower().endswith("pump"):
                return c
        # Otherwise the longest candidate is most likely the mint.
        return max(candidates, key=len)

    @staticmethod
    async def _lookup_ca(ca: str) -> str:
        try:
            from ..ingestion.scanner import scanner

            coin = await scanner.snapshot(ca, pumpfun_only=False)
            if not coin:
                return ""
            return (
                f"{coin.name} ({coin.symbol}) age {coin.age_hours:.0f}h, "
                f"mcap ${coin.market_cap_usd:,.0f}, liq ${coin.liquidity_usd:,.0f}, "
                f"24h {coin.change_h24:+.0f}%, twitter={coin.twitter or '-'}"
            )
        except Exception:
            return ""


advice_evaluator = AdviceEvaluator()
knowledge_processor = KnowledgeProcessor()
