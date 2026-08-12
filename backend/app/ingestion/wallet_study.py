"""On-chain study of a trader's wallet via Helius (free signal, no X quota).

The richest, cheapest way to judge a KOL is their actual wallet, not their tweets. Helius'
enhanced transactions API returns human-readable swap history, so we can estimate how
active a wallet is and whether it's a real trader vs a dormant/steaming address. We keep
this deliberately lightweight (recent activity summary), not a full PnL engine.
"""

from __future__ import annotations

import httpx

from ..config import settings
from .helius_budget import helius_budget


class WalletStudy:
    def __init__(self) -> None:
        self._key = settings.helius_api_key

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def summarize(self, wallet: str) -> dict | None:
        """Return a light activity summary for a wallet, or None if unavailable."""
        if not wallet or not self._key:
            return None
        # Respect the global Helius budget; skip rather than risk the credit.
        if not helius_budget.can_spend(1):
            return None
        url = (
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
            f"?api-key={self._key}&limit=50"
        )
        try:
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(url)
                helius_budget.spend(1)
                r.raise_for_status()
                txs = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(txs, list) or not txs:
            return {"wallet": wallet, "txs": 0, "swaps": 0, "active": False, "note": "no recent activity"}

        # Count real trading: explicit swaps OR pump.fun / pumpswap / dex trades (which
        # Helius often labels type=UNKNOWN but source=PUMP_FUN / PUMPSWAP / RAYDIUM etc).
        dex_sources = {"PUMP_FUN", "PUMPSWAP", "RAYDIUM", "JUPITER", "METEORA", "ORCA"}
        trades = sum(
            1 for t in txs
            if t.get("type") == "SWAP" or t.get("source") in dex_sources
        )
        # Timestamps are unix seconds; measure span of the recent window.
        stamps = [t.get("timestamp", 0) for t in txs if t.get("timestamp")]
        span_h = (max(stamps) - min(stamps)) / 3600.0 if len(stamps) > 1 else 0.0
        per_day = round(len(txs) / (span_h / 24.0), 1) if span_h > 1 else len(txs)
        return {
            "wallet": wallet,
            "txs": len(txs),
            "swaps": trades,
            "active": trades >= 5,
            "tx_per_day": per_day,
            "note": f"{trades} trades in last {len(txs)} txs (~{per_day}/day activity)",
        }


wallet_study = WalletStudy()
