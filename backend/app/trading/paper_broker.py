"""Paper broker — simulates fills with realistic fees + slippage.

Models the frictions that actually kill memecoin PnL: pump.fun's ~1% fee, priority
fees, and slippage that scales inversely with liquidity. This is what makes paper
results trustworthy enough to gate the switch to real money.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

PUMP_FEE_PCT = 0.01          # ~1% platform fee each side
PRIORITY_FEE_USD = 0.02      # rough priority/network fee per tx
BASE_SLIPPAGE_PCT = 0.02


@dataclass
class Fill:
    price: float
    tokens: float
    cost_usd: float          # cash actually moved (incl. fees) on buy
    proceeds_usd: float      # cash received (net fees) on sell
    fees_usd: float


def _slippage(liquidity_usd: float, size_usd: float) -> float:
    """More size relative to liquidity => worse fill."""
    if liquidity_usd <= 0:
        return 0.25
    impact = size_usd / liquidity_usd
    slip = BASE_SLIPPAGE_PCT + impact * 0.5 + random.uniform(0, 0.01)
    return min(0.30, slip)


def buy(price: float, size_usd: float, liquidity_usd: float) -> Fill:
    slip = _slippage(liquidity_usd, size_usd)
    fill_price = price * (1 + slip)
    fee = size_usd * PUMP_FEE_PCT + PRIORITY_FEE_USD
    invest = max(0.0, size_usd - fee)
    tokens = invest / fill_price if fill_price > 0 else 0.0
    return Fill(
        price=fill_price,
        tokens=tokens,
        cost_usd=size_usd,
        proceeds_usd=0.0,
        fees_usd=fee,
    )


def sell(price: float, tokens: float, liquidity_usd: float) -> Fill:
    gross = price * tokens
    slip = _slippage(liquidity_usd, gross)
    fill_price = price * (1 - slip)
    gross = fill_price * tokens
    fee = gross * PUMP_FEE_PCT + PRIORITY_FEE_USD
    proceeds = max(0.0, gross - fee)
    return Fill(
        price=fill_price,
        tokens=tokens,
        cost_usd=0.0,
        proceeds_usd=proceeds,
        fees_usd=fee,
    )
