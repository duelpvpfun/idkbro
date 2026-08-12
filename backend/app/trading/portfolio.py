"""Portfolio + open positions.

Tracks cash and open positions, executes paper buys/trims/sells through the broker
(fees + slippage + gas modeled), records every fill, and keeps the running state the
position brain needs to reason: peak multiple, recent buy/sell flow, time held.

Exits are NOT decided here — the position brain owns that. This class only executes what
the brain decides and books the resulting PnL. Total performance is derived from equity
(cash + marked-to-market open positions) vs the starting bankroll, so accounting can't
drift out of sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..db import repository as repo
from ..domain import Observation
from ..events import EventType, bus
from . import paper_broker


@dataclass
class Position:
    trade_id: int
    mint: str
    symbol: str
    entry_price: float
    entry_mcap: float
    tokens_initial: float
    tokens_remaining: float
    cost_basis_usd: float          # original cash cost (incl. fees)
    proceeds_usd: float = 0.0      # cash realized from trims/sells so far
    opened_ts: float = field(default_factory=time.time)
    peak_price: float = 0.0
    plan: str = ""
    creator_wallet: str = ""
    obs: Observation | None = None
    recent_buys: int = 0
    recent_sells: int = 0
    last_price: float = 0.0
    source: str = "launch"          # launch / revival
    liquidity_hint: float = 0.0     # for revivals we mark liquidity from the scanner

    def multiple(self, price: float) -> float:
        return price / self.entry_price if self.entry_price else 1.0

    def peak_multiple(self) -> float:
        return self.peak_price / self.entry_price if self.entry_price else 1.0


class Portfolio:
    def __init__(self, starting_cash: float) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.total_fees = 0.0

    async def load_from_db(self) -> int:
        """Rebuild cash, fees, and open positions from the persisted ledger.

        Lets the agent survive restarts/reboots without losing live paper trades. Cash is
        reconstructed from the fill ledger so it stays exactly consistent with history.
        Returns the number of open positions restored.
        """
        self.cash, self.total_fees = await repo.cash_and_fees_from_fills(self.starting_cash)
        self.positions = {}
        for t in await repo.open_trades():
            pos = Position(
                trade_id=t.id, mint=t.mint, symbol=t.symbol,
                entry_price=t.entry_price, entry_mcap=t.entry_mcap_usd,
                tokens_initial=t.tokens_initial, tokens_remaining=t.tokens_remaining,
                cost_basis_usd=t.size_usd, proceeds_usd=t.realized_usd,
                opened_ts=t.opened_ts, peak_price=t.entry_price * max(1.0, t.peak_multiple),
                plan=t.plan, creator_wallet=t.creator_wallet, obs=None,
                last_price=t.entry_price,
            )
            self.positions[t.mint] = pos
        return len(self.positions)

    def open_count(self) -> int:
        return len(self.positions)

    def equity(self, price_lookup) -> float:
        value = self.cash
        for pos in self.positions.values():
            price = price_lookup(pos.mint) or pos.last_price or pos.entry_price
            value += price * pos.tokens_remaining
        return value

    @property
    def realized_pnl(self) -> float:
        """Cash change vs start, adjusted for capital still tied up in open positions."""
        invested = sum(p.cost_basis_usd - p.proceeds_usd for p in self.positions.values())
        return round(self.cash - self.starting_cash + invested, 2)

    # --- open ------------------------------------------------------------
    async def open_position(
        self, obs: Observation, size_usd: float, conviction: float,
        thesis, rationale: str, plan: str,
    ) -> Position | None:
        launch = obs.launch
        if launch.mint in self.positions:
            return None
        price = obs.price_now or launch.initial_price_usd
        if price <= 0:
            return None
        fill = paper_broker.buy(price, size_usd, obs.liquidity_usd)
        if fill.tokens <= 0:
            return None
        self.cash -= fill.cost_usd
        self.total_fees += fill.fees_usd

        trade = await repo.create_trade(
            mint=launch.mint, symbol=launch.symbol, status="OPEN",
            entry_price=fill.price, entry_mcap_usd=obs.market_cap_usd,
            size_usd=fill.cost_usd, tokens_initial=fill.tokens, tokens_remaining=fill.tokens,
            conviction=conviction, thesis_category=thesis.category, thesis=thesis.summary,
            rationale=rationale, plan=plan,
            dev_holding_pct=obs.dev_holding_pct, sniped_pct=obs.sniped_pct,
            smart_buyer_score=obs.smart_buyer_score, creator_wallet=launch.creator_wallet,
        )
        await repo.add_fill(
            trade_id=trade.id, kind="BUY", price=fill.price, tokens=fill.tokens,
            usd=fill.cost_usd, fees_usd=fill.fees_usd, reason=rationale[:200],
        )
        pos = Position(
            trade_id=trade.id, mint=launch.mint, symbol=launch.symbol,
            entry_price=fill.price, entry_mcap=obs.market_cap_usd,
            tokens_initial=fill.tokens, tokens_remaining=fill.tokens,
            cost_basis_usd=fill.cost_usd, peak_price=fill.price, plan=plan,
            creator_wallet=launch.creator_wallet, obs=obs, last_price=fill.price,
        )
        self.positions[launch.mint] = pos
        await bus.emit(
            EventType.TRADE, kind="OPEN", symbol=pos.symbol, mint=pos.mint,
            price=fill.price, size_usd=round(fill.cost_usd, 2),
            conviction=round(conviction, 2), cash=round(self.cash, 2), source="launch",
        )
        return pos

    async def open_from_market(
        self, coin, size_usd: float, conviction: float, thesis, rationale: str, plan: str,
    ) -> Position | None:
        """Open a position on an existing (old) coin flagged as a revival."""
        if coin.mint in self.positions:
            return None
        price = coin.price_usd
        if price <= 0:
            return None
        fill = paper_broker.buy(price, size_usd, coin.liquidity_usd)
        if fill.tokens <= 0:
            return None
        self.cash -= fill.cost_usd
        self.total_fees += fill.fees_usd

        trade = await repo.create_trade(
            mint=coin.mint, symbol=coin.symbol, status="OPEN",
            entry_price=fill.price, entry_mcap_usd=coin.market_cap_usd,
            size_usd=fill.cost_usd, tokens_initial=fill.tokens, tokens_remaining=fill.tokens,
            conviction=conviction, thesis_category=thesis.category, thesis=thesis.summary,
            rationale=rationale, plan=plan,
            dev_holding_pct=0.0, sniped_pct=0.0, smart_buyer_score=0.0, creator_wallet="",
        )
        await repo.add_fill(
            trade_id=trade.id, kind="BUY", price=fill.price, tokens=fill.tokens,
            usd=fill.cost_usd, fees_usd=fill.fees_usd, reason=rationale[:200],
        )
        pos = Position(
            trade_id=trade.id, mint=coin.mint, symbol=coin.symbol,
            entry_price=fill.price, entry_mcap=coin.market_cap_usd,
            tokens_initial=fill.tokens, tokens_remaining=fill.tokens,
            cost_basis_usd=fill.cost_usd, peak_price=fill.price, plan=plan,
            creator_wallet="", obs=None, last_price=fill.price,
        )
        pos.source = "revival"
        pos.liquidity_hint = coin.liquidity_usd
        self.positions[coin.mint] = pos
        await bus.emit(
            EventType.TRADE, kind="OPEN", symbol=pos.symbol, mint=pos.mint,
            price=fill.price, size_usd=round(fill.cost_usd, 2),
            conviction=round(conviction, 2), cash=round(self.cash, 2), source="revival",
        )
        return pos

    # --- trim / sell -----------------------------------------------------
    async def trim(self, mint: str, fraction: float, price: float, liquidity: float, reason: str):
        pos = self.positions.get(mint)
        if pos is None:
            return None
        fraction = max(0.0, min(1.0, fraction))
        tokens = pos.tokens_remaining * fraction
        if tokens <= 0:
            return None
        fill = paper_broker.sell(price, tokens, liquidity)
        self.cash += fill.proceeds_usd
        self.total_fees += fill.fees_usd
        pos.proceeds_usd += fill.proceeds_usd
        pos.tokens_remaining -= tokens

        await repo.add_fill(
            trade_id=pos.trade_id, kind="TRIM", price=fill.price, tokens=tokens,
            usd=fill.proceeds_usd, fees_usd=fill.fees_usd, reason=reason[:200],
        )
        await repo.update_trade(
            pos.trade_id, tokens_remaining=pos.tokens_remaining, realized_usd=pos.proceeds_usd,
        )
        await bus.emit(
            EventType.TRADE, kind="TRIM", symbol=pos.symbol, mint=mint, price=fill.price,
            fraction=round(fraction, 2), proceeds=round(fill.proceeds_usd, 2),
            reason=reason, cash=round(self.cash, 2),
        )
        if pos.tokens_remaining <= pos.tokens_initial * 0.01:
            return await self._finalize(mint, price, "trimmed_out")
        return None

    async def sell(self, mint: str, price: float, liquidity: float, reason: str):
        pos = self.positions.get(mint)
        if pos is None:
            return None
        fill = paper_broker.sell(price, pos.tokens_remaining, liquidity)
        self.cash += fill.proceeds_usd
        self.total_fees += fill.fees_usd
        pos.proceeds_usd += fill.proceeds_usd
        pos.tokens_remaining = 0.0
        await repo.add_fill(
            trade_id=pos.trade_id, kind="SELL", price=fill.price, tokens=0.0,
            usd=fill.proceeds_usd, fees_usd=fill.fees_usd, reason=reason[:200],
        )
        return await self._finalize(mint, fill.price, reason)

    async def _finalize(self, mint: str, exit_price: float, reason: str):
        pos = self.positions.pop(mint, None)
        if pos is None:
            return None
        pnl_usd = pos.proceeds_usd - pos.cost_basis_usd
        pnl_pct = pnl_usd / pos.cost_basis_usd if pos.cost_basis_usd else 0.0
        await repo.update_trade(
            pos.trade_id, status="CLOSED", closed_ts=time.time(), exit_price=exit_price,
            exit_reason=reason, pnl_usd=pnl_usd, pnl_pct=pnl_pct,
            peak_multiple=pos.peak_multiple(), tokens_remaining=0.0,
            realized_usd=pos.proceeds_usd,
        )
        won = pnl_usd > 0
        await bus.emit(
            EventType.TRADE, kind="CLOSE", symbol=pos.symbol, mint=mint, price=exit_price,
            reason=reason, pnl_usd=round(pnl_usd, 2), pnl_pct=round(pnl_pct * 100, 1),
            peak_x=round(pos.peak_multiple(), 2), cash=round(self.cash, 2),
        )
        return pos, won
