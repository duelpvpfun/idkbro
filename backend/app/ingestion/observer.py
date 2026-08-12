"""Observation window.

When a token launches, the agent doesn't buy blind — it *watches* for a short window,
accumulating trade ticks to measure the things that actually predict rugs vs runners on
pump.fun: how much the dev/snipers grabbed, whether buys outweigh sells, how many unique
buyers, whether any known smart-money wallets aped in early, and the early price path.

Bundle/sniper estimate: pump.fun launches are frequently sniped by bundled wallets in
the first block. We approximate "sniped %" as the share of buy volume captured in the
first ~2s by wallets other than the creator. It's a heuristic (exactly like the terminals
that over-tag bundles), so it feeds judgment rather than being an absolute gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..domain import Observation, TokenLaunch, TradeTick


@dataclass
class _Window:
    launch: TokenLaunch
    started: float
    ticks: list[TradeTick] = field(default_factory=list)


class ObservationCollector:
    def __init__(self, window_seconds: float) -> None:
        self.window_seconds = window_seconds
        self._open: dict[str, _Window] = {}

    def begin(self, launch: TokenLaunch) -> None:
        self._open[launch.mint] = _Window(launch=launch, started=time.time())

    def is_watching(self, mint: str) -> bool:
        return mint in self._open

    def add_tick(self, tick: TradeTick) -> None:
        w = self._open.get(tick.mint)
        if w is not None:
            w.ticks.append(tick)

    def due(self) -> list[str]:
        now = time.time()
        return [
            mint for mint, w in self._open.items()
            if now - w.started >= self.window_seconds
        ]

    def buyer_wallets(self, mint: str) -> list[str]:
        """Distinct buyer wallets seen so far (to look up smart-money scores)."""
        w = self._open.get(mint)
        if w is None:
            return []
        return list({t.wallet for t in w.ticks if t.is_buy and t.wallet})

    @staticmethod
    def _detect_bundles(buys, creator, started, cluster_ms: float = 0.35):
        """Detect bundled buys: distinct wallets buying within the same tiny time
        cluster near launch. A bundle is a single transaction funding many wallets, so
        their trades share (near) identical timestamps. Returns (tokens, wallet_count)
        for the largest such cluster in the first ~3s.

        This is a heuristic — like Axiom/Photon/BullX terminals, it over-tags sometimes
        (many wallets get flagged as bundles even when they aren't), so the agent uses it
        as a risk signal for judgment, not as absolute truth.
        """
        window_buys = sorted(
            (t for t in buys if t.wallet != creator and t.ts <= started + 3.0),
            key=lambda t: t.ts,
        )
        best_tokens = 0.0
        best_wallets = 0
        i = 0
        n = len(window_buys)
        while i < n:
            j = i
            wallets: set[str] = set()
            tokens = 0.0
            while j < n and window_buys[j].ts - window_buys[i].ts <= cluster_ms:
                wallets.add(window_buys[j].wallet)
                tokens += window_buys[j].token_amount
                j += 1
            # Only call it a bundle if >=3 distinct wallets fired together.
            if len(wallets) >= 3 and tokens > best_tokens:
                best_tokens = tokens
                best_wallets = len(wallets)
            i += 1
        return best_tokens, best_wallets

    def finalize(self, mint: str, smart_scores: dict[str, float]) -> Observation | None:
        w = self._open.pop(mint, None)
        if w is None:
            return None
        return self._build(w, smart_scores)

    def _build(self, w: _Window, smart_scores: dict[str, float]) -> Observation:
        ticks = w.ticks
        buys = [t for t in ticks if t.is_buy]
        sells = [t for t in ticks if not t.is_buy]
        buyers = {t.wallet for t in buys}
        creator = w.launch.creator_wallet

        total_supply = 1_000_000_000.0
        # Dev holding: use the creation-tx buy (initialBuy) as the reliable baseline. Only
        # ADD later creator buys that happen clearly AFTER launch (>1.5s), so we don't
        # double-count the creation buy when it also arrives as an early trade tick.
        dev_late_tokens = sum(
            t.token_amount for t in buys
            if t.wallet == creator and t.ts > w.started + 1.5
        )
        dev_pct = w.launch.dev_initial_pct + dev_late_tokens / total_supply * 100.0
        # If we never got an initialBuy figure, fall back to all creator buys in-window.
        if w.launch.dev_initial_pct <= 0:
            dev_pct = sum(
                t.token_amount for t in buys if t.wallet == creator
            ) / total_supply * 100.0
        dev_pct = min(100.0, dev_pct)

        # Snipers/bundlers: on pump.fun the snipe happens in the SAME block as (or the
        # block right after) creation — realistically within ~1s. Trading bots do it
        # "perfectly", so we treat the first ~1s of non-creator buys as the snipe bundle,
        # and also detect obvious bundles: a burst of buys sharing (near) identical
        # timestamps, which is the fingerprint of a bundled multi-wallet transaction.
        snipe_cut = w.started + 1.0
        early_buys = [t for t in buys if t.ts <= snipe_cut and t.wallet != creator]
        early_tokens = sum(t.token_amount for t in early_buys)

        bundle_tokens, bundle_wallets = self._detect_bundles(buys, creator, w.started)
        # Sniped % = max of the time-window estimate and the bundle-cluster estimate,
        # since a well-timed bot bundle may land a hair outside the 1s window.
        sniped_pct = min(
            100.0,
            max(early_tokens, bundle_tokens) / total_supply * 100.0,
        )
        bundled = max(len({t.wallet for t in early_buys}), bundle_wallets)

        # Smart-money early buyers.
        smart_early = [wl for wl in buyers if smart_scores.get(wl, 0.0) > 0.4]
        smart_score = (
            sum(smart_scores.get(wl, 0.0) for wl in smart_early) / len(smart_early)
            if smart_early else 0.0
        )

        prices = [t.price_usd for t in ticks if t.price_usd > 0]
        launch_price = w.launch.initial_price_usd or (prices[0] if prices else 0.0)
        price_now = prices[-1] if prices else launch_price
        peak = max(prices) if prices else launch_price
        change = (price_now - launch_price) / launch_price if launch_price else 0.0

        last = ticks[-1] if ticks else None
        return Observation(
            launch=w.launch,
            window_seconds=self.window_seconds,
            buys=len(buys),
            sells=len(sells),
            unique_buyers=len(buyers),
            dev_holding_pct=round(dev_pct, 2),
            sniped_pct=round(sniped_pct, 2),
            bundled_wallets=bundled,
            smart_buyers=smart_early,
            smart_buyer_score=round(smart_score, 3),
            price_now=price_now,
            price_change_pct=round(change * 100, 2),
            peak_price=peak,
            liquidity_usd=w.launch.initial_liquidity_usd,
            market_cap_usd=last.market_cap_usd if last else 0.0,
            volume_usd=sum(t.sol_amount for t in ticks),
            holders=len(buyers),
        )
