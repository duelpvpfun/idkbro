"""Shared domain types passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class TokenLaunch:
    """The moment a token is created on pump.fun."""

    mint: str
    symbol: str
    name: str
    created_ts: float
    creator_wallet: str = ""
    description: str = ""
    image: Optional[str] = None
    twitter: Optional[str] = None
    website: Optional[str] = None
    telegram: Optional[str] = None
    metadata_uri: Optional[str] = None    # IPFS JSON with description + socials
    initial_price_usd: float = 0.0
    initial_liquidity_usd: float = 0.0
    # % of supply the dev grabbed in the creation tx itself (initialBuy).
    dev_initial_pct: float = 0.0
    metadata_loaded: bool = False


@dataclass
class TradeTick:
    """A single buy/sell observed on the bonding curve."""

    mint: str
    ts: float
    is_buy: bool
    wallet: str
    sol_amount: float
    token_amount: float
    price_usd: float
    market_cap_usd: float


@dataclass
class Observation:
    """What the agent measured during the observation window after launch."""

    launch: TokenLaunch
    window_seconds: float
    buys: int = 0
    sells: int = 0
    unique_buyers: int = 0
    dev_holding_pct: float = 0.0
    sniped_pct: float = 0.0            # % of supply grabbed in the first moments
    bundled_wallets: int = 0
    top10_holding_pct: float = 0.0     # holder concentration (bubblemap-style), -1 if unknown
    smart_buyers: list[str] = field(default_factory=list)  # tracked good wallets early in
    smart_buyer_score: float = 0.0    # aggregate reputation of early buyers
    price_now: float = 0.0
    price_change_pct: float = 0.0     # since launch, over the window
    peak_price: float = 0.0
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    volume_usd: float = 0.0
    holders: int = 0


@dataclass
class MarketCoin:
    """A snapshot of an already-existing (not brand-new) pump.fun coin from the market
    scanner. Built from DexScreener data so the agent can spot revivals of old coins."""

    mint: str
    symbol: str
    name: str
    age_hours: float
    price_usd: float
    market_cap_usd: float
    liquidity_usd: float
    # Price change % over rolling windows.
    change_m5: float = 0.0
    change_h1: float = 0.0
    change_h6: float = 0.0
    change_h24: float = 0.0
    # Volume in USD over rolling windows.
    vol_m5: float = 0.0
    vol_h1: float = 0.0
    vol_h6: float = 0.0
    vol_h24: float = 0.0
    buys_h1: int = 0
    sells_h1: int = 0
    migrated: bool = False             # graduated to PumpSwap AMM
    description: str = ""
    twitter: Optional[str] = None
    website: Optional[str] = None
    is_pumpfun: bool = True


@dataclass
class RevivalSignal:
    """Why the agent thinks an old coin is waking back up."""

    waking: bool
    strength: float = 0.0              # 0..1 how strong the revival looks
    reasons: list[str] = field(default_factory=list)
    pattern: str = ""                  # e.g. 'volume_spike', 'reclaim', 'breakout'


@dataclass
class SafetyReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class Thesis:
    """The agent's understanding of what a coin IS."""

    category: str = "unknown"          # meme, animal, political, tech, celebrity, ...
    summary: str = ""                  # plain-language "what is this"
    narrative_strength: float = 0.5    # 0..1 how compelling / sticky the narrative is
    virality: float = 0.5              # 0..1 meme/shareability potential
    red_flags: list[str] = field(default_factory=list)


class Action(str, Enum):
    BUY = "BUY"
    SKIP = "SKIP"


@dataclass
class EntryDecision:
    action: Action
    conviction: float                  # 0..1
    size_pct: float                    # fraction of equity (pre-risk bounds)
    thesis_tag: str
    rationale: str
    # The agent's own plan, in its words — NOT hard rules the system enforces.
    plan: str = ""
    tags: list[str] = field(default_factory=list)


class ManageAction(str, Enum):
    HOLD = "HOLD"
    TRIM = "TRIM"      # sell part
    SELL = "SELL"      # exit fully
    ADD = "ADD"        # scale in more


@dataclass
class ManageDecision:
    action: ManageAction
    trim_fraction: float = 0.0         # for TRIM: fraction of remaining tokens to sell
    reason: str = ""
