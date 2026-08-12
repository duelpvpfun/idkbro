"""SQLAlchemy ORM models — the agent's long-term memory."""

from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """One position (open or closed) with the agent's thesis and outcome.

    The agent manages exits itself (trim/sell), so there are no system-enforced TP/SL
    columns — only what actually happened plus the agent's own plan in `plan`.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN / CLOSED

    # Entry
    opened_ts: Mapped[float] = mapped_column(Float, default=time.time)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_mcap_usd: Mapped[float] = mapped_column(Float, default=0.0)
    size_usd: Mapped[float] = mapped_column(Float)          # original cost basis
    tokens_initial: Mapped[float] = mapped_column(Float)
    tokens_remaining: Mapped[float] = mapped_column(Float)
    realized_usd: Mapped[float] = mapped_column(Float, default=0.0)  # from trims so far

    # Understanding + conviction
    conviction: Mapped[float] = mapped_column(Float, default=0.0)
    thesis_category: Mapped[str] = mapped_column(String(32), default="unknown")
    thesis: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[str] = mapped_column(Text, default="")     # agent's own words

    # Observed launch context (for learning what wins/rugs)
    dev_holding_pct: Mapped[float] = mapped_column(Float, default=0.0)
    sniped_pct: Mapped[float] = mapped_column(Float, default=0.0)
    smart_buyer_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Exit
    closed_ts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    peak_multiple: Mapped[float] = mapped_column(Float, default=1.0)  # best x reached
    exit_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    postmortem: Mapped[str] = mapped_column(Text, default="")

    creator_wallet: Mapped[str] = mapped_column(String(64), default="")


class Fill(Base):
    """A single paper fill (buy / trim / sell) belonging to a trade."""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    kind: Mapped[str] = mapped_column(String(8))            # BUY / TRIM / SELL / ADD
    price: Mapped[float] = mapped_column(Float)
    tokens: Mapped[float] = mapped_column(Float)
    usd: Mapped[float] = mapped_column(Float)               # cash moved (net fees)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")


class WalletStat(Base):
    """Smart-money DB: reputation of creator + trader wallets."""

    __tablename__ = "wallet_stats"

    wallet: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="trader")  # creator / trader
    tokens_created: Mapped[int] = mapped_column(Integer, default=0)
    early_hits: Mapped[int] = mapped_column(Integer, default=0)   # early on a winner
    early_misses: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    total_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0)      # 0..1 reputation
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_ts: Mapped[float] = mapped_column(Float, default=time.time)


class Lesson(Base):
    """A lesson the agent extracted during reflection."""

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_ts: Mapped[float] = mapped_column(Float, default=time.time)
    category: Mapped[str] = mapped_column(String(32), default="general")
    text: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class MarketKnowledge(Base):
    """Context the user teaches ('this coin ran because a KOL quoted it'). This is NOT a
    rule the agent obeys. It's remembered background so the agent can recognize a similar
    setup/narrative later. The agent decides how much weight to give it; it never becomes
    a hard instruction. Different from AdviceNote, which changes behavior when adopted."""

    __tablename__ = "market_knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_ts: Mapped[float] = mapped_column(Float, default=time.time)
    topic: Mapped[str] = mapped_column(String(64), default="")      # e.g. coin ticker / narrative
    text: Mapped[str] = mapped_column(Text)                          # what the user explained
    takeaway: Mapped[str] = mapped_column(Text, default="")         # agent's own paraphrase of the pattern
    category: Mapped[str] = mapped_column(String(32), default="general")


class Playbook(Base):
    """The agent's living strategy document — a single evolving row it rewrites.

    This is the closest thing to the agent's 'trading philosophy'. Reflection updates it
    over time so behavior is shaped by an accumulated, human-readable strategy rather
    than only opaque weights.
    """

    __tablename__ = "playbook"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_ts: Mapped[float] = mapped_column(Float, default=time.time)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")


class AdviceNote(Base):
    """User advice + the agent's OWN verdict on whether to adopt it.

    The user offers insight; the agent critically evaluates it against its track record
    and decides its stance (adopt / partial / reject). It is never forced to obey — this
    row records both what you said and what it decided to do about it.
    """

    __tablename__ = "advice_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_ts: Mapped[float] = mapped_column(Float, default=time.time)
    mint: Mapped[str] = mapped_column(String(64), default="", index=True)
    text: Mapped[str] = mapped_column(Text)                       # the advice you gave
    stance: Mapped[str] = mapped_column(String(16), default="pending")  # adopt/partial/reject/pending
    reasoning: Mapped[str] = mapped_column(Text, default="")      # why it decided that
    adopted_rule: Mapped[str] = mapped_column(Text, default="")   # what it actually took, if any
    active: Mapped[int] = mapped_column(Integer, default=0)       # 1 if folded into style

class WatchlistCoin(Base):
    """The agent's market memory — every pump.fun coin it has seen, whether it bought,
    sold, or skipped it. The scanner revisits these to catch revivals, and coins it
    skipped that later run become lessons ('the ones that got away').
    """

    __tablename__ = "watchlist"

    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(64), default="")
    first_seen_ts: Mapped[float] = mapped_column(Float, default=time.time)
    last_seen_ts: Mapped[float] = mapped_column(Float, default=time.time)
    # Where it entered our world and what we did.
    source: Mapped[str] = mapped_column(String(16), default="launch")  # launch / revival
    disposition: Mapped[str] = mapped_column(String(16), default="skipped")  # bought/sold/skipped/holding
    # Market state for revival detection + learning.
    mcap_at_seen: Mapped[float] = mapped_column(Float, default=0.0)
    peak_mcap_seen: Mapped[float] = mapped_column(Float, default=0.0)
    last_mcap: Mapped[float] = mapped_column(Float, default=0.0)
    thesis_category: Mapped[str] = mapped_column(String(32), default="unknown")
    thesis: Mapped[str] = mapped_column(Text, default="")
    # How many times we've re-evaluated it as a possible revival.
    revival_checks: Mapped[int] = mapped_column(Integer, default=0)
    last_revival_ts: Mapped[float] = mapped_column(Float, default=0.0)
    # Set when a coin we skipped later mooned — flagged for a lesson.
    missed_runner: Mapped[int] = mapped_column(Integer, default=0)
    skip_reason: Mapped[str] = mapped_column(Text, default="")        # why he passed on it
    postmortem_done: Mapped[int] = mapped_column(Integer, default=0)  # analyzed the miss yet
    notes: Mapped[str] = mapped_column(Text, default="")


class Identity(Base):
    """The agent's self-chosen public identity. It decides these itself; we just apply
    the name + bio to X. pfp/banner are stored as the concept the agent wants (an image
    still has to be produced separately)."""

    __tablename__ = "identity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    updated_ts: Mapped[float] = mapped_column(Float, default=time.time)
    chosen: Mapped[int] = mapped_column(Integer, default=0)     # 1 once the agent has decided
    display_name: Mapped[str] = mapped_column(String(50), default="")
    bio: Mapped[str] = mapped_column(String(160), default="")
    pfp_concept: Mapped[str] = mapped_column(Text, default="")  # what it wants its avatar to be
    banner_concept: Mapped[str] = mapped_column(Text, default="")
    applied_to_x: Mapped[int] = mapped_column(Integer, default=0)


class TrackedTrader(Base):
    """A pump.fun trader/KOL the agent may choose to track. WE feed candidates (real data
    only); the AGENT decides whether to trust/follow each one and why. Never auto-trusted."""

    __tablename__ = "tracked_traders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_ts: Mapped[float] = mapped_column(Float, default=time.time)
    x_handle: Mapped[str] = mapped_column(String(32), default="", index=True)
    wallet: Mapped[str] = mapped_column(String(64), default="", index=True)
    label: Mapped[str] = mapped_column(String(64), default="")      # how they were described to it
    source: Mapped[str] = mapped_column(String(32), default="user") # user / discovered
    # The agent's own verdict.
    stance: Mapped[str] = mapped_column(String(16), default="pending")  # trust/watch/ignore/pending
    reasoning: Mapped[str] = mapped_column(Text, default="")
    followed_on_x: Mapped[int] = mapped_column(Integer, default=0)
    pfp_url: Mapped[str] = mapped_column(String(256), default="")   # their avatar (for style inspiration)
    notes: Mapped[str] = mapped_column(Text, default="")