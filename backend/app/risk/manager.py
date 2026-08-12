"""Risk manager — bounds the brain's sizing and enforces concurrency.

Per the user: no daily loss limit. The agent is free to have losing days while it learns.
This layer only keeps single-trade size sane (never above the hard cap) and prevents
over-diversifying into too many positions at once. It can still be paused via kill switch.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass
class RiskVerdict:
    approved: bool
    size_usd: float
    reason: str


class RiskManager:
    def __init__(self) -> None:
        self.kill_switch = False

    def check(
        self, size_pct: float, cash_usd: float, equity_usd: float, open_positions: int
    ) -> RiskVerdict:
        if self.kill_switch:
            return RiskVerdict(False, 0.0, "kill switch active")
        if open_positions >= settings.max_concurrent_positions:
            return RiskVerdict(False, 0.0, "max concurrent positions reached")

        capped = min(size_pct, settings.size_hard_cap)
        size = min(capped * equity_usd, cash_usd)
        if size < 1.0:
            return RiskVerdict(False, 0.0, "insufficient cash for a meaningful size")
        return RiskVerdict(True, round(size, 2), f"approved {capped:.0%} of equity")
