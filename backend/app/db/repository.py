"""Data-access helpers over the ORM models."""

from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import select

from .database import SessionLocal, write_lock
from .models import (
    AdviceNote,
    Fill,
    Identity,
    Lesson,
    MarketKnowledge,
    MigratedCoin,
    Playbook,
    TrackedTrader,
    Trade,
    WalletStat,
    WatchlistCoin,
)


def _migration_key(name: str, symbol: str) -> str:
    """Normalized identity key for a migrated coin (case/space-insensitive)."""
    return f"{(name or '').strip().lower()}|{(symbol or '').strip().lower()}"

DEFAULT_PLAYBOOK = """# Trading philosophy (v1 — starting instincts, will evolve)

I trade brand-new pump.fun launches with paper money to learn the real market.

## What I look for
- A clear, sticky narrative I actually understand (meme, animal, tech, culture moment).
- Healthy launch: dev not dumping, low sniper/bundle concentration, organic buyers.
- Bonus conviction when wallets with a good track record buy early.

## Sizing
- Normal ideas: 5-10% of equity.
- High-conviction ideas: 11-18%. Never blow past ~20% on one coin.

## Managing (my call, no fixed rules)
- Some coins go 5k->5m, others 10k->100k and die. I decide per coin.
- Trim into strength to de-risk, let a real runner run, cut fast when the thesis breaks
  or buy pressure dies. I am not married to any position.

## How I improve
- Every trade teaches me. I write down what worked and what fooled me, and I update
  this playbook so tomorrow's me is sharper than today's.
"""


# ------------------------------------------------------------------ trades
async def create_trade(**kwargs) -> Trade:
    async with write_lock, SessionLocal() as s:
        trade = Trade(**kwargs)
        s.add(trade)
        await s.commit()
        await s.refresh(trade)
        return trade


async def get_trade(trade_id: int) -> Optional[Trade]:
    async with SessionLocal() as s:
        return await s.get(Trade, trade_id)


async def update_trade(trade_id: int, **fields) -> Optional[Trade]:
    async with write_lock, SessionLocal() as s:
        trade = await s.get(Trade, trade_id)
        if trade is None:
            return None
        for k, v in fields.items():
            setattr(trade, k, v)
        await s.commit()
        await s.refresh(trade)
        return trade


async def add_fill(**kwargs) -> None:
    async with write_lock, SessionLocal() as s:
        s.add(Fill(**kwargs))
        await s.commit()


async def recent_trades(limit: int = 100) -> list[Trade]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(Trade).order_by(Trade.opened_ts.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def open_trades() -> list[Trade]:
    async with SessionLocal() as s:
        result = await s.execute(select(Trade).where(Trade.status == "OPEN"))
        return list(result.scalars().all())


async def closed_trades(limit: int = 500) -> list[Trade]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(Trade)
            .where(Trade.status == "CLOSED")
            .order_by(Trade.closed_ts.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def all_trades() -> list[Trade]:
    async with SessionLocal() as s:
        result = await s.execute(select(Trade))
        return list(result.scalars().all())


async def fills_for(trade_id: int) -> list[Fill]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(Fill).where(Fill.trade_id == trade_id).order_by(Fill.ts.asc())
        )
        return list(result.scalars().all())


async def cash_and_fees_from_fills(starting_cash: float) -> tuple[float, float]:
    """Reconstruct current cash + total fees purely from the fill ledger.

    BUY fills move cash out (usd is the cost incl. fees); TRIM/SELL fills move cash in
    (usd is the net proceeds). Fees are summed separately for reporting.
    """
    async with SessionLocal() as s:
        result = await s.execute(select(Fill))
        fills = list(result.scalars().all())
    cash = starting_cash
    fees = 0.0
    for f in fills:
        if f.kind in ("BUY", "ADD"):
            cash -= f.usd
        else:  # TRIM / SELL
            cash += f.usd
        fees += f.fees_usd
    return round(cash, 6), round(fees, 6)


# ------------------------------------------------------------------ wallets
async def _get_or_make_wallet(s, wallet: str, role: str) -> WalletStat:
    stat = await s.get(WalletStat, wallet)
    if stat is None:
        stat = WalletStat(
            wallet=wallet, role=role, tokens_created=0, early_hits=0, early_misses=0,
            wins=0, losses=0, total_pnl_usd=0.0, score=0.0,
        )
        s.add(stat)
    return stat


def _recompute_score(stat: WalletStat) -> None:
    hits = stat.early_hits + stat.wins
    total = hits + stat.early_misses + stat.losses
    win_rate = hits / total if total else 0.0
    pnl_bonus = 1.0 if stat.total_pnl_usd > 0 else 0.0
    stat.score = round(min(1.0, 0.7 * win_rate + 0.3 * pnl_bonus), 4)


async def register_creator(wallet: str) -> None:
    if not wallet:
        return
    async with write_lock, SessionLocal() as s:
        stat = await _get_or_make_wallet(s, wallet, "creator")
        stat.tokens_created += 1
        stat.updated_ts = time.time()
        await s.commit()


async def record_early_buyer(wallet: str, won: bool) -> None:
    """A wallet that bought early on a token we later judged win/loss."""
    if not wallet:
        return
    async with write_lock, SessionLocal() as s:
        stat = await _get_or_make_wallet(s, wallet, "trader")
        if won:
            stat.early_hits += 1
        else:
            stat.early_misses += 1
        _recompute_score(stat)
        stat.updated_ts = time.time()
        await s.commit()


async def wallet_scores(wallets: list[str]) -> dict[str, float]:
    if not wallets:
        return {}
    async with SessionLocal() as s:
        result = await s.execute(
            select(WalletStat).where(WalletStat.wallet.in_(wallets))
        )
        return {w.wallet: w.score for w in result.scalars().all()}


async def good_wallets(limit: int = 30, min_score: float = 0.0) -> list[WalletStat]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(WalletStat)
            .where(WalletStat.score >= min_score)
            .order_by(WalletStat.score.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


# ------------------------------------------------------------------ lessons
async def add_lesson(text: str, category: str = "general", weight: float = 1.0) -> None:
    async with write_lock, SessionLocal() as s:
        s.add(Lesson(text=text, category=category, weight=weight))
        await s.commit()


async def recent_lessons(limit: int = 12) -> list[Lesson]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(Lesson).order_by(Lesson.created_ts.desc()).limit(limit)
        )
        return list(result.scalars().all())


# ------------------------------------------------------------------ market knowledge
async def add_market_knowledge(
    text: str, topic: str = "", takeaway: str = "", category: str = "general"
) -> MarketKnowledge:
    async with write_lock, SessionLocal() as s:
        note = MarketKnowledge(text=text, topic=topic, takeaway=takeaway, category=category)
        s.add(note)
        await s.commit()
        await s.refresh(note)
        return note


async def set_knowledge_takeaway(note_id: int, takeaway: str) -> None:
    async with write_lock, SessionLocal() as s:
        note = await s.get(MarketKnowledge, note_id)
        if note is not None:
            note.takeaway = takeaway
            await s.commit()


async def recent_market_knowledge(limit: int = 12) -> list[MarketKnowledge]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(MarketKnowledge).order_by(MarketKnowledge.created_ts.desc()).limit(limit)
        )
        return list(result.scalars().all())


# ------------------------------------------------------------------ playbook
async def get_playbook() -> Playbook:
    async with write_lock, SessionLocal() as s:
        pb = await s.get(Playbook, 1)
        if pb is None:
            pb = Playbook(id=1, version=1, content=DEFAULT_PLAYBOOK, updated_ts=time.time())
            s.add(pb)
            await s.commit()
            await s.refresh(pb)
        return pb


async def update_playbook(content: str) -> Playbook:
    async with write_lock, SessionLocal() as s:
        pb = await s.get(Playbook, 1)
        if pb is None:
            pb = Playbook(id=1, version=1, content=content, updated_ts=time.time())
            s.add(pb)
        else:
            pb.content = content
            pb.version += 1
            pb.updated_ts = time.time()
        await s.commit()
        await s.refresh(pb)
        return pb


# ------------------------------------------------------------------ advice
async def add_advice(text: str, mint: str = "") -> AdviceNote:
    async with write_lock, SessionLocal() as s:
        note = AdviceNote(text=text, mint=mint, stance="pending")
        s.add(note)
        await s.commit()
        await s.refresh(note)
        return note


async def set_advice_verdict(
    advice_id: int, stance: str, reasoning: str, adopted_rule: str
) -> None:
    async with write_lock, SessionLocal() as s:
        note = await s.get(AdviceNote, advice_id)
        if note is None:
            return
        note.stance = stance
        note.reasoning = reasoning
        note.adopted_rule = adopted_rule
        note.active = 1 if stance in ("adopt", "partial") and adopted_rule else 0
        await s.commit()


async def active_advice(limit: int = 15) -> list[AdviceNote]:
    """Advice the agent chose to fold into its trading style."""
    async with SessionLocal() as s:
        result = await s.execute(
            select(AdviceNote)
            .where(AdviceNote.active == 1)
            .order_by(AdviceNote.created_ts.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def recent_advice(limit: int = 30) -> list[AdviceNote]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(AdviceNote).order_by(AdviceNote.created_ts.desc()).limit(limit)
        )
        return list(result.scalars().all())

# ------------------------------------------------------------------ watchlist
async def upsert_watch(
    mint: str,
    *,
    symbol: str = "",
    name: str = "",
    source: str = "launch",
    disposition: str | None = None,
    mcap: float = 0.0,
    thesis_category: str = "",
    thesis: str = "",
    skip_reason: str = "",
) -> None:
    """Remember a coin (or update what we know). Called for launches AND revivals,
    whether we bought or skipped — this is the agent's market memory."""
    async with write_lock, SessionLocal() as s:
        coin = await s.get(WatchlistCoin, mint)
        now = time.time()
        if coin is None:
            coin = WatchlistCoin(
                mint=mint, symbol=symbol, name=name, first_seen_ts=now,
                source=source, mcap_at_seen=mcap, peak_mcap_seen=mcap, last_mcap=mcap,
                thesis_category=thesis_category or "unknown", thesis=thesis,
                disposition=disposition or "skipped", skip_reason=skip_reason[:500],
            )
            s.add(coin)
        else:
            coin.last_seen_ts = now
            if skip_reason and not coin.skip_reason:
                coin.skip_reason = skip_reason[:500]
            if symbol:
                coin.symbol = symbol
            if name:
                coin.name = name
            if mcap:
                coin.last_mcap = mcap
                coin.peak_mcap_seen = max(coin.peak_mcap_seen, mcap)
            if disposition:
                coin.disposition = disposition
            if thesis_category:
                coin.thesis_category = thesis_category
            if thesis:
                coin.thesis = thesis
        await s.commit()


async def mark_revival_check(mint: str, mcap: float) -> None:
    async with write_lock, SessionLocal() as s:
        coin = await s.get(WatchlistCoin, mint)
        if coin is None:
            return
        coin.revival_checks += 1
        coin.last_revival_ts = time.time()
        if mcap:
            coin.last_mcap = mcap
            coin.peak_mcap_seen = max(coin.peak_mcap_seen, mcap)
        await s.commit()


async def flag_missed_runner(mint: str) -> bool:
    """Flag a skipped coin that ran. Returns True the FIRST time (new miss), so the caller
    can trigger a one-time post-mortem."""
    async with write_lock, SessionLocal() as s:
        coin = await s.get(WatchlistCoin, mint)
        if coin is not None and not coin.missed_runner:
            coin.missed_runner = 1
            await s.commit()
            return True
        return False


async def mark_postmortem_done(mint: str) -> None:
    async with write_lock, SessionLocal() as s:
        coin = await s.get(WatchlistCoin, mint)
        if coin is not None:
            coin.postmortem_done = 1
            await s.commit()


async def get_watch(mint: str) -> Optional[WatchlistCoin]:
    async with SessionLocal() as s:
        return await s.get(WatchlistCoin, mint)


async def record_migration(mint: str, symbol: str, name: str) -> None:
    """Remember that a coin migrated (graduated). Its name/ticker is now 'spent' so
    fresh redeploys reusing it can be skipped."""
    async with write_lock, SessionLocal() as s:
        row = await s.get(MigratedCoin, mint)
        if row is None:
            s.add(MigratedCoin(
                mint=mint, symbol=symbol or "", name=name or "",
                key=_migration_key(name, symbol), migrated_ts=time.time(),
            ))
            await s.commit()


async def was_name_migrated(name: str, symbol: str, within_days: float = 30.0) -> Optional[MigratedCoin]:
    """Return a recent migrated coin whose name+ticker matches (a fresh launch is a
    redeploy of an already-graduated identity), else None. A symbol match alone isn't
    enough (tickers get reused across unrelated coins), so we require the name too."""
    key = _migration_key(name, symbol)
    if not name or not symbol:
        return None
    cutoff = time.time() - within_days * 86_400
    async with SessionLocal() as s:
        result = await s.execute(
            select(MigratedCoin)
            .where(MigratedCoin.key == key)
            .where(MigratedCoin.migrated_ts >= cutoff)
            .order_by(MigratedCoin.migrated_ts.desc())
            .limit(1)
        )
        return result.scalars().first()


async def recent_migrations(limit: int = 20) -> list[MigratedCoin]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(MigratedCoin).order_by(MigratedCoin.migrated_ts.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def watchlist_for_revival(limit: int = 60, max_age_days: float = 14) -> list[WatchlistCoin]:
    """Coins worth re-checking for a revival: seen before, not currently bought/holding,
    not ancient. Prioritizes ones we haven't checked recently."""
    cutoff = time.time() - max_age_days * 86_400
    async with SessionLocal() as s:
        result = await s.execute(
            select(WatchlistCoin)
            .where(WatchlistCoin.first_seen_ts >= cutoff)
            .where(WatchlistCoin.disposition.in_(("skipped", "sold")))
            .order_by(WatchlistCoin.last_revival_ts.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def recent_missed_runners(limit: int = 10) -> list[WatchlistCoin]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(WatchlistCoin)
            .where(WatchlistCoin.missed_runner == 1)
            .order_by(WatchlistCoin.last_seen_ts.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


# ------------------------------------------------------------------ identity
async def get_identity() -> Identity:
    async with SessionLocal() as s:
        ident = await s.get(Identity, 1)
        if ident is None:
            ident = Identity(id=1)
            s.add(ident)
            await s.commit()
            await s.refresh(ident)
        return ident


async def set_identity(
    display_name: str, bio: str, pfp_concept: str, banner_concept: str
) -> Identity:
    async with write_lock, SessionLocal() as s:
        ident = await s.get(Identity, 1)
        if ident is None:
            ident = Identity(id=1)
            s.add(ident)
        ident.display_name = display_name[:50]
        ident.bio = bio[:160]
        ident.pfp_concept = pfp_concept
        ident.banner_concept = banner_concept
        ident.chosen = 1
        ident.updated_ts = time.time()
        await s.commit()
        await s.refresh(ident)
        return ident


async def mark_identity_applied() -> None:
    async with write_lock, SessionLocal() as s:
        ident = await s.get(Identity, 1)
        if ident is not None:
            ident.applied_to_x = 1
            await s.commit()


# ------------------------------------------------------------------ tracked traders
async def add_tracked_trader(
    x_handle: str, wallet: str, label: str, source: str = "user"
) -> TrackedTrader:
    async with write_lock, SessionLocal() as s:
        t = TrackedTrader(
            x_handle=x_handle.lstrip("@")[:32], wallet=wallet[:64],
            label=label[:64], source=source, stance="pending",
        )
        s.add(t)
        await s.commit()
        await s.refresh(t)
        return t


async def set_trader_verdict(trader_id: int, stance: str, reasoning: str) -> None:
    async with write_lock, SessionLocal() as s:
        t = await s.get(TrackedTrader, trader_id)
        if t is None:
            return
        t.stance = stance
        t.reasoning = reasoning
        await s.commit()


async def mark_trader_followed(trader_id: int) -> None:
    async with write_lock, SessionLocal() as s:
        t = await s.get(TrackedTrader, trader_id)
        if t is not None:
            t.followed_on_x = 1
            await s.commit()


async def set_trader_pfp(trader_id: int, pfp_url: str) -> None:
    async with write_lock, SessionLocal() as s:
        t = await s.get(TrackedTrader, trader_id)
        if t is not None:
            t.pfp_url = pfp_url[:256]
            await s.commit()


async def find_trader(x_handle: str = "", wallet: str = "") -> Optional[TrackedTrader]:
    async with SessionLocal() as s:
        q = select(TrackedTrader)
        if x_handle:
            q = q.where(TrackedTrader.x_handle == x_handle.lstrip("@"))
        elif wallet:
            q = q.where(TrackedTrader.wallet == wallet)
        else:
            return None
        result = await s.execute(q.limit(1))
        return result.scalars().first()


async def set_trader_style(trader_id: int, style: str, wallet: str = "") -> None:
    """Store the user's explanation of a trader's style; also fill in the wallet if given
    and the record didn't have one (e.g. was imported handle-only)."""
    async with write_lock, SessionLocal() as s:
        t = await s.get(TrackedTrader, trader_id)
        if t is not None:
            t.notes = style[:1000]
            if wallet and not t.wallet:
                t.wallet = wallet[:64]
            await s.commit()


async def trusted_pfps(limit: int = 6) -> list[str]:
    """Avatar URLs of traders the agent rates, for visual style inspiration."""
    async with SessionLocal() as s:
        result = await s.execute(
            select(TrackedTrader)
            .where(TrackedTrader.stance.in_(("trust", "watch")))
            .where(TrackedTrader.pfp_url != "")
            .limit(limit)
        )
        return [t.pfp_url for t in result.scalars().all() if t.pfp_url]


async def pending_traders(limit: int = 20) -> list[TrackedTrader]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(TrackedTrader).where(TrackedTrader.stance == "pending").limit(limit)
        )
        return list(result.scalars().all())


async def trusted_traders(limit: int = 50) -> list[TrackedTrader]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(TrackedTrader)
            .where(TrackedTrader.stance.in_(("trust", "watch")))
            .order_by(TrackedTrader.created_ts.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def all_traders(limit: int = 200) -> list[TrackedTrader]:
    async with SessionLocal() as s:
        result = await s.execute(
            select(TrackedTrader).order_by(TrackedTrader.created_ts.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def trusted_wallet_set() -> dict[str, str]:
    """Map of wallet -> stance for traders the agent trusts/watches (for buy-time lookup)."""
    async with SessionLocal() as s:
        result = await s.execute(
            select(TrackedTrader).where(TrackedTrader.stance.in_(("trust", "watch")))
        )
        return {t.wallet: t.stance for t in result.scalars().all() if t.wallet}