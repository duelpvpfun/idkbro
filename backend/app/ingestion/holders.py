"""Holder-concentration check via Helius (the practical 'bubblemaps' signal).

Full bubblemaps-style cluster analysis (linking wallets by funding relationships) needs
heavy graph work and a paid data provider. The high-value 80/20 version — and what most
rug checks actually use — is **top-holder concentration**: if a handful of wallets hold
most of the supply, they can dump on you regardless of how the chart looks.

This uses Helius `getTokenLargestAccounts` (standard Solana RPC, included with any Helius
key) to compute the top-10 holders' share of supply. If no key is set it returns -1
(unknown) and the agent simply proceeds on its other signals.
"""

from __future__ import annotations

import httpx

from ..config import settings
from .helius_budget import helius_budget

_PUMP_SUPPLY = 1_000_000_000.0


class HolderCheck:
    def __init__(self) -> None:
        self._url = (
            f"https://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
            if settings.helius_api_key
            else ""
        )

    @property
    def available(self) -> bool:
        return bool(self._url)

    async def top10_pct(self, mint: str) -> float:
        """Return top-10 holders' % of supply, or -1.0 if unavailable.

        Note: on a brand-new pump.fun token the bonding-curve account itself holds most
        of the supply; we exclude the single largest account (the curve) so the number
        reflects concentration among actual holders.
        """
        if not self._url:
            return -1.0
        if not helius_budget.can_spend(1):
            return -1.0
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(
                    self._url,
                    json={
                        "jsonrpc": "2.0",
                        "id": "holders",
                        "method": "getTokenLargestAccounts",
                        "params": [mint],
                    },
                )
                helius_budget.spend(1)
                r.raise_for_status()
                vals = (r.json().get("result") or {}).get("value") or []
                if not vals:
                    return -1.0
                amounts = sorted(
                    (float(v.get("uiAmount") or 0) for v in vals), reverse=True
                )
                # Drop the largest (bonding curve) then take next 10.
                holders = amounts[1:11] if len(amounts) > 1 else amounts
                top10 = sum(holders)
                return round(min(100.0, top10 / _PUMP_SUPPLY * 100.0), 2)
        except (httpx.HTTPError, ValueError, KeyError):
            return -1.0


holder_check = HolderCheck()
