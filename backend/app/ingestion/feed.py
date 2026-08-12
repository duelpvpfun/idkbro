"""Live pump.fun feed via PumpPortal websocket.

Two streams the agent needs:
  * new token launches (subscribeNewToken)
  * per-token trades on the bonding curve (subscribeTokenTrade) so we can measure the
    observation window: who's buying, snipers/bundles, dev selling, price trajectory.

Events are normalized into TokenLaunch / TradeTick and pushed onto an asyncio.Queue the
agent consumes. SOL is priced in USD via a cached DexScreener lookup so PnL is real-money
meaningful. If the socket drops it reconnects with backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Optional

import httpx
import websockets

from ..config import settings
from ..domain import TokenLaunch, TradeTick

SOL_MINT = "So11111111111111111111111111111111111111112"
_PUMP_SUPPLY = 1_000_000_000  # pump.fun tokens have a fixed 1B supply

# Public IPFS gateways to try in order (pump.fun stores metadata on IPFS).
_IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://pump.mypinata.cloud/ipfs/",
]


def _normalize_ipfs(uri: str) -> list[str]:
    """Return candidate HTTP URLs for an ipfs:// or gateway URI."""
    if not uri:
        return []
    cid = None
    if uri.startswith("ipfs://"):
        cid = uri[len("ipfs://") :].lstrip("/")
    elif "/ipfs/" in uri:
        cid = uri.split("/ipfs/", 1)[1]
    if cid:
        return [g + cid for g in _IPFS_GATEWAYS]
    return [uri]  # already a plain https URL


async def load_metadata(uri: str) -> dict:
    """Fetch the token's off-chain metadata JSON (description + socials).

    pump.fun's websocket only sends name/symbol/uri — the human-readable description and
    social links live in this JSON. Without it every coin looks blank, so this is what
    lets the agent actually understand what a coin is. Tries multiple IPFS gateways.
    """
    for url in _normalize_ipfs(uri):
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    return data
        except (httpx.HTTPError, ValueError):
            continue
    return {}


class SolPrice:
    """Cheap cached SOL/USD price (refreshed occasionally)."""

    def __init__(self) -> None:
        self._price = 150.0
        self._ts = 0.0

    async def get(self) -> float:
        if time.time() - self._ts < 60:
            return self._price
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(
                    "https://api.dexscreener.com/latest/dex/tokens/" + SOL_MINT
                )
                r.raise_for_status()
                pairs = r.json().get("pairs") or []
                usd = next((float(p["priceUsd"]) for p in pairs if p.get("priceUsd")), None)
                if usd:
                    self._price = usd
                    self._ts = time.time()
        except (httpx.HTTPError, ValueError, KeyError, StopIteration):
            pass
        return self._price


class PumpPortalFeed:
    def __init__(self) -> None:
        self.url = settings.pumpportal_ws_url
        self.launches: asyncio.Queue[TokenLaunch] = asyncio.Queue()
        self.trades: asyncio.Queue[TradeTick] = asyncio.Queue()
        # Coins that graduated off the bonding curve (migrated to PumpSwap).
        self.migrations: asyncio.Queue[dict] = asyncio.Queue()
        self.sol = SolPrice()
        self._watched: set[str] = set()
        self._ws = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.url, ping_interval=20) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    # Graduations: a coin migrating means its name/ticker is now spent.
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    # Re-subscribe to any tokens we're still watching.
                    if self._watched:
                        await ws.send(
                            json.dumps(
                                {"method": "subscribeTokenTrade", "keys": list(self._watched)}
                            )
                        )
                    backoff = 1
                    async for raw in ws:
                        await self._handle(raw)
            except (websockets.WebSocketException, OSError):
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def watch_trades(self, mint: str) -> None:
        self._watched.add(mint)
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(
                    json.dumps({"method": "subscribeTokenTrade", "keys": [mint]})
                )

    async def unwatch_trades(self, mint: str) -> None:
        self._watched.discard(mint)
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(
                    json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]})
                )

    async def _handle(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        txtype = msg.get("txType")

        # Migration/graduation event: coin left the bonding curve for PumpSwap.
        if txtype in ("migrate", "migration") or msg.get("pool") == "pump-amm":
            mint = msg.get("mint")
            if mint:
                await self.migrations.put({
                    "mint": mint,
                    "symbol": msg.get("symbol", "") or "",
                    "name": msg.get("name", "") or "",
                })
            return

        sol_usd = await self.sol.get()

        if txtype == "create" or msg.get("pool") == "pump" and "name" in msg and "mint" in msg:
            await self.launches.put(self._to_launch(msg, sol_usd))
        elif txtype in ("buy", "sell"):
            tick = self._to_tick(msg, sol_usd)
            if tick is not None:
                await self.trades.put(tick)

    @staticmethod
    def _mcap_usd(msg: dict, sol_usd: float) -> float:
        mc_sol = float(msg.get("marketCapSol", 0) or 0)
        return mc_sol * sol_usd

    def _to_launch(self, msg: dict, sol_usd: float) -> TokenLaunch:
        mcap = self._mcap_usd(msg, sol_usd)
        price = mcap / _PUMP_SUPPLY if mcap else 0.0
        # Dev's creation-tx buy as % of the fixed 1B supply.
        dev_tokens = float(msg.get("initialBuy", 0) or 0)
        dev_pct = min(100.0, dev_tokens / _PUMP_SUPPLY * 100.0) if dev_tokens else 0.0
        return TokenLaunch(
            mint=msg.get("mint", ""),
            symbol=msg.get("symbol", "???"),
            name=msg.get("name", "Unknown"),
            created_ts=time.time(),
            creator_wallet=msg.get("traderPublicKey", "") or "",
            description=msg.get("description", "") or "",
            image=msg.get("image"),
            twitter=msg.get("twitter"),
            website=msg.get("website"),
            telegram=msg.get("telegram"),
            metadata_uri=msg.get("uri"),
            initial_price_usd=price,
            initial_liquidity_usd=float(msg.get("vSolInBondingCurve", 0) or 0) * sol_usd,
            dev_initial_pct=round(dev_pct, 2),
        )

    def _to_tick(self, msg: dict, sol_usd: float) -> Optional[TradeTick]:
        mint = msg.get("mint")
        if not mint:
            return None
        mcap = self._mcap_usd(msg, sol_usd)
        price = mcap / _PUMP_SUPPLY if mcap else 0.0
        return TradeTick(
            mint=mint,
            ts=time.time(),
            is_buy=msg.get("txType") == "buy",
            wallet=msg.get("traderPublicKey", "") or "",
            sol_amount=float(msg.get("solAmount", 0) or 0),
            token_amount=float(msg.get("tokenAmount", 0) or 0),
            price_usd=price,
            market_cap_usd=mcap,
        )
