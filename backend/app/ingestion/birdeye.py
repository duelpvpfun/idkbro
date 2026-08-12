"""Birdeye client — richer market data than DexScreener.

Gives the agent closer to what pro terminals see: real holder counts, token overview
(price, liquidity, mcap, 24h volume), and a security check (mint/freeze authority, top-10
holder concentration, LP). Budget-guarded so it can't burn your credit, with graceful
fallback to None when no key or budget is exhausted (the agent then uses its other data).
"""

from __future__ import annotations

import time

import httpx

from ..config import settings

_BASE = "https://public-api.birdeye.so"


class _Budget:
    def __init__(self) -> None:
        self._day = int(time.time() // 86_400)
        self._used = 0

    def _roll(self) -> None:
        d = int(time.time() // 86_400)
        if d != self._day:
            self._day = d
            self._used = 0

    def can(self) -> bool:
        self._roll()
        return self._used < settings.birdeye_max_calls_per_day

    def spend(self) -> None:
        self._roll()
        self._used += 1

    @property
    def used(self) -> int:
        self._roll()
        return self._used


class Birdeye:
    def __init__(self) -> None:
        self.budget = _Budget()

    @property
    def available(self) -> bool:
        return bool(settings.birdeye_api_key)

    def _headers(self) -> dict:
        return {
            "X-API-KEY": settings.birdeye_api_key,
            "x-chain": settings.birdeye_chain,
            "accept": "application/json",
        }

    async def _get(self, path: str, params: dict) -> dict | None:
        if not self.available or not self.budget.can():
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(_BASE + path, params=params, headers=self._headers())
                self.budget.spend()
                r.raise_for_status()
                data = r.json()
                return data.get("data") if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def overview(self, mint: str) -> dict | None:
        """Token overview: price, liquidity, mcap, volume, holder count."""
        d = await self._get("/defi/token_overview", {"address": mint})
        if not d:
            return None
        return {
            "price": d.get("price"),
            "liquidity": d.get("liquidity"),
            "mcap": d.get("mc") or d.get("marketCap"),
            "v24h": d.get("v24hUSD"),
            "holders": d.get("holder"),
            "priceChange24h": d.get("priceChange24hPercent"),
        }

    async def holder_count(self, mint: str) -> int | None:
        d = await self._get("/defi/token_overview", {"address": mint})
        if d and d.get("holder") is not None:
            return int(d["holder"])
        return None

    async def security(self, mint: str) -> dict | None:
        """Token security: authorities, top-10 concentration, LP status."""
        d = await self._get("/defi/token_security", {"address": mint})
        if not d:
            return None
        return {
            "mint_authority": d.get("mintAuthority"),
            "freeze_authority": d.get("freezeAuthority"),
            "top10_holder_pct": d.get("top10HolderPercent"),
            "lp_burned_pct": d.get("lpBurnPercent"),
            "is_mutable": d.get("mutableMetadata"),
        }


birdeye = Birdeye()
