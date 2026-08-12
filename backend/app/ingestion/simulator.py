"""Offline simulator that mimics the PumpPortal feed.

Produces the SAME TokenLaunch + TradeTick streams as the live feed so the entire agent
(observation window, thesis, entry, position management, learning) runs end-to-end with
no keys. Each token has a hidden regime (rug / bleed / runner / moon) that drives its
trade flow and price so the agent has realistic material to learn from.
"""

from __future__ import annotations

import asyncio
import random
import string
import time

from ..config import settings
from ..domain import TokenLaunch, TradeTick

_ADJ = ["Turbo", "Based", "Giga", "Moon", "Doge", "Pepe", "Chad", "Baby", "Sigma", "Quantum"]
_NOUN = ["Inu", "Cat", "Coin", "Rocket", "Floki", "AI", "Bonk", "Frog", "Bull", "Agent"]
_THEMES = [
    "a dog-themed community coin",
    "an AI agent token that trades autonomously",
    "a political meme about the election",
    "a frog meme reviving an old classic",
    "a cat coin with a viral TikTok",
    "a tech/DePIN narrative coin",
    "a celebrity tribute coin",
    "a low-effort cash-grab with no description",
]
_SUPPLY = 1_000_000_000


def _rand(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


class _SimToken:
    def __init__(self) -> None:
        adj, noun = random.choice(_ADJ), random.choice(_NOUN)
        self.symbol = (adj[:3] + noun[:3]).upper()
        self.name = f"{adj} {noun}"
        self.mint = _rand(44)
        self.creator = _rand(44)
        self.created = time.time()
        self.theme = random.choice(_THEMES)
        self.has_socials = "cash-grab" not in self.theme and random.random() > 0.3

        r = random.random()
        self.regime = "rug" if r < 0.6 else "bleed" if r < 0.85 else "runner" if r < 0.97 else "moon"
        rug_like = self.regime in ("rug", "bleed")

        self.price = random.uniform(0.00000003, 0.0000002)
        self.mcap = self.price * _SUPPLY
        self.dev_hold = random.uniform(6, 20) if rug_like else random.uniform(0, 7)
        self.sniped = random.uniform(12, 45) if rug_like else random.uniform(0, 18)

    def launch(self) -> TokenLaunch:
        return TokenLaunch(
            mint=self.mint,
            symbol=self.symbol,
            name=self.name,
            created_ts=self.created,
            creator_wallet=self.creator,
            description=self.theme if self.has_socials else "",
            twitter=f"https://x.com/{self.symbol.lower()}" if self.has_socials else None,
            website=None,  # skip network fetch in sim
            initial_price_usd=self.price,
            initial_liquidity_usd=random.uniform(3_000, 25_000),
            dev_initial_pct=round(self.dev_hold, 2),
            metadata_loaded=True,  # sim provides everything inline
        )

    def step_price(self) -> None:
        drift = {"rug": -0.05, "bleed": -0.012, "runner": 0.035, "moon": 0.08}[self.regime]
        change = random.gauss(drift, 0.07)
        if self.regime == "rug" and random.random() < 0.04:
            change = -0.8
        self.price = max(1e-12, self.price * (1 + change))
        self.mcap = self.price * _SUPPLY

    def tick(self, is_buy: bool | None = None, wallet: str | None = None) -> TradeTick:
        if is_buy is None:
            is_buy = random.random() < (0.35 if self.regime in ("rug", "bleed") else 0.65)
        sol = random.uniform(0.05, 3.0)
        return TradeTick(
            mint=self.mint,
            ts=time.time(),
            is_buy=is_buy,
            wallet=wallet or _rand(44),
            sol_amount=sol,
            token_amount=sol * 1e6,
            price_usd=self.price,
            market_cap_usd=self.mcap,
        )


class SimulatedFeed:
    """Drop-in replacement for PumpPortalFeed."""

    def __init__(self) -> None:
        self.launches: asyncio.Queue[TokenLaunch] = asyncio.Queue()
        self.trades: asyncio.Queue[TradeTick] = asyncio.Queue()
        self.migrations: asyncio.Queue[dict] = asyncio.Queue()  # unused in sim, keeps API parity
        self._tokens: dict[str, _SimToken] = {}
        self._watched: set[str] = set()
        self._running = False
        # A pool of recurring "smart money" wallets that tend to hit winners.
        self._smart_wallets = [_rand(44) for _ in range(8)]

    async def run(self) -> None:
        self._running = True
        await asyncio.gather(self._spawn_loop(), self._trade_loop())

    async def stop(self) -> None:
        self._running = False

    async def watch_trades(self, mint: str) -> None:
        self._watched.add(mint)

    async def unwatch_trades(self, mint: str) -> None:
        self._watched.discard(mint)

    async def _spawn_loop(self) -> None:
        interval = 60.0 / max(1, settings.sim_tokens_per_minute)
        while self._running:
            tok = _SimToken()
            self._tokens[tok.mint] = tok
            await self.launches.put(tok.launch())
            # Emit the initial snipe/dev burst so the observation window can measure it.
            for _ in range(random.randint(1, 6)):
                w = None
                # Smart wallets more likely to snipe eventual winners early.
                if tok.regime in ("runner", "moon") and random.random() < 0.5:
                    w = random.choice(self._smart_wallets)
                await self.trades.put(tok.tick(is_buy=True, wallet=w))
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))

    async def _trade_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.4)
            for mint in list(self._tokens.keys()):
                tok = self._tokens[mint]
                tok.step_price()
                if mint in self._watched and random.random() < 0.7:
                    w = None
                    if tok.regime in ("runner", "moon") and random.random() < 0.3:
                        w = random.choice(self._smart_wallets)
                    await self.trades.put(tok.tick(wallet=w))
            # Forget very old tokens to bound memory.
            if len(self._tokens) > 300:
                for m in list(self._tokens.keys())[:100]:
                    self._tokens.pop(m, None)

    def price_of(self, mint: str) -> float | None:
        tok = self._tokens.get(mint)
        return tok.price if tok else None
