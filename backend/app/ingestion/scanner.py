"""Market scanner — the agent's eyes on OLD coins, not just fresh launches.

A real trader watches the whole market and keeps a watchlist, because old coins often
roar back to life. This scanner runs on a slow loop (~60s) and produces MarketCoin
snapshots from two sources:

  1. Discovery — DexScreener trending/boosted Solana tokens (filtered to pump.fun coins).
  2. Memory — re-checking coins already on our watchlist (ones we skipped or sold).

DexScreener is free and gives everything we need to spot a revival: multi-window price
change, volume, age, liquidity, mcap. Pump.fun coins are identified by the `pump` suffix
convention on the mint and/or a PumpSwap/pump dexId.
"""

from __future__ import annotations

import time

import httpx

from ..domain import MarketCoin

_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"
_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"


def _is_pumpfun(mint: str, pair: dict | None = None) -> bool:
    """Pump.fun mints conventionally end in 'pump'. Migrated coins trade on PumpSwap."""
    if mint.lower().endswith("pump"):
        return True
    if pair:
        dex = str(pair.get("dexId", "")).lower()
        if "pump" in dex:
            return True
    return False


class MarketScanner:
    def __init__(self) -> None:
        self._client_kwargs = {"timeout": 12, "follow_redirects": True}

    async def trending_pumpfun(self, limit: int = 40) -> list[str]:
        """Mints of currently trending/boosted pump.fun coins (discovery)."""
        mints: list[str] = []
        try:
            async with httpx.AsyncClient(**self._client_kwargs) as c:
                for url in (_BOOSTS_URL, _PROFILES_URL):
                    try:
                        r = await c.get(url)
                        r.raise_for_status()
                        for item in r.json():
                            if item.get("chainId") != "solana":
                                continue
                            addr = item.get("tokenAddress", "")
                            if addr and _is_pumpfun(addr):
                                mints.append(addr)
                    except (httpx.HTTPError, ValueError):
                        continue
        except httpx.HTTPError:
            pass
        # De-dup, cap.
        seen: set[str] = set()
        out: list[str] = []
        for m in mints:
            if m not in seen:
                seen.add(m)
                out.append(m)
            if len(out) >= limit:
                break
        return out

    async def top_runners(self, limit: int = 25) -> list[dict]:
        """Trending Solana coins with name + performance, for learning the current meta.

        Returns lightweight dicts {symbol, name, mcap, change_h24, age_hours}. Not filtered
        to pump.fun so the agent sees the whole market's meta, which is what shapes what
        new pump.fun launches will be riffing on.
        """
        mints: list[str] = []
        try:
            async with httpx.AsyncClient(**self._client_kwargs) as c:
                for url in (_BOOSTS_URL, _PROFILES_URL):
                    try:
                        r = await c.get(url)
                        r.raise_for_status()
                        for item in r.json():
                            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                                mints.append(item["tokenAddress"])
                    except (httpx.HTTPError, ValueError):
                        continue
        except httpx.HTTPError:
            return []

        out: list[dict] = []
        seen: set[str] = set()
        for mint in mints:
            if mint in seen or len(out) >= limit:
                continue
            seen.add(mint)
            coin = await self.snapshot(mint, pumpfun_only=False)
            if coin and coin.symbol != "???":
                out.append({
                    "symbol": coin.symbol, "name": coin.name,
                    "mcap": coin.market_cap_usd, "change_h24": coin.change_h24,
                    "age_hours": coin.age_hours,
                })
        return out

    async def snapshot(self, mint: str, pumpfun_only: bool = True) -> MarketCoin | None:
        """Full MarketCoin snapshot for one mint from DexScreener.

        pumpfun_only filters out non-pump coins for the trading scanner. For a direct CA
        lookup (e.g. the user teaching about a specific coin), pass pumpfun_only=False so
        the agent can study any token it's shown, migrated or not.
        """
        try:
            async with httpx.AsyncClient(**self._client_kwargs) as c:
                r = await c.get(_TOKENS_URL + mint)
                r.raise_for_status()
                pairs = r.json().get("pairs") or []
        except (httpx.HTTPError, ValueError):
            return None
        if not pairs:
            return None
        # Use the most-liquid pair.
        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        if pumpfun_only and not _is_pumpfun(mint, pair):
            return None
        return self._to_coin(mint, pair)

    def _to_coin(self, mint: str, p: dict) -> MarketCoin:
        base = p.get("baseToken") or {}
        pc = p.get("priceChange") or {}
        vol = p.get("volume") or {}
        txns = p.get("txns") or {}
        h1tx = txns.get("h1") or {}
        created_ms = p.get("pairCreatedAt") or 0
        age_hours = (time.time() - created_ms / 1000.0) / 3600.0 if created_ms else 0.0
        info = p.get("info") or {}
        socials = {s.get("type"): s.get("url") for s in (info.get("socials") or [])}
        websites = info.get("websites") or []
        dex = str(p.get("dexId", "")).lower()
        return MarketCoin(
            mint=mint,
            symbol=base.get("symbol", "???"),
            name=base.get("name", "Unknown"),
            age_hours=round(age_hours, 2),
            price_usd=float(p.get("priceUsd", 0) or 0),
            market_cap_usd=float(p.get("marketCap", 0) or p.get("fdv", 0) or 0),
            liquidity_usd=float((p.get("liquidity") or {}).get("usd", 0) or 0),
            change_m5=float(pc.get("m5", 0) or 0),
            change_h1=float(pc.get("h1", 0) or 0),
            change_h6=float(pc.get("h6", 0) or 0),
            change_h24=float(pc.get("h24", 0) or 0),
            vol_m5=float(vol.get("m5", 0) or 0),
            vol_h1=float(vol.get("h1", 0) or 0),
            vol_h6=float(vol.get("h6", 0) or 0),
            vol_h24=float(vol.get("h24", 0) or 0),
            buys_h1=int(h1tx.get("buys", 0) or 0),
            sells_h1=int(h1tx.get("sells", 0) or 0),
            migrated="pumpswap" in dex or "raydium" in dex,
            description=str(info.get("description", "") or ""),
            twitter=socials.get("twitter"),
            website=websites[0].get("url") if websites else None,
            is_pumpfun=True,
        )


scanner = MarketScanner()
