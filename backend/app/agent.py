"""The agent runtime — a live, always-on pump.fun trader that learns.

Pipeline per launch:
  launch → begin observation window → collect trade ticks (~20s) → safety gate
  → understand (thesis) → score smart-money → entry brain (conviction + size)
  → risk bounds → paper open. Then a manage loop watches every open position and lets
  the position brain decide hold/trim/sell on its own. Reflection periodically rewrites
  the playbook. Everything streams to the dashboard.

Runs against the live PumpPortal feed by default; falls back to the simulator when
USE_SIMULATOR=true (or for offline dev).
"""

from __future__ import annotations

import asyncio
import time

from .brain.entry import EntryBrain
from .brain.llm import llm
from .brain.meta import meta_tracker
from .brain.position import PositionBrain
from .brain.revival import RevivalBrain, RevivalDetector
from .brain.understanding import UnderstandingEngine
from .brain.wallets import WalletTracker
from .config import settings
from .db import repository as repo
from .domain import Action, ManageAction, TokenLaunch, TradeTick
from .events import EventType, bus
from .ingestion.birdeye import birdeye
from .ingestion.feed import PumpPortalFeed
from .ingestion.holders import holder_check
from .ingestion.observer import ObservationCollector
from .ingestion.scanner import scanner
from .ingestion.simulator import SimulatedFeed
from .learning.reflection import ReflectionEngine
from .risk.manager import RiskManager
from .safety import checks
from .social.x_poster import x_poster
from .trading.portfolio import Portfolio


class Agent:
    def __init__(self) -> None:
        self.portfolio = Portfolio(settings.starting_bankroll_usd)
        self.risk = RiskManager()
        self.observer = ObservationCollector(settings.observation_seconds)
        self.understanding = UnderstandingEngine()
        self.entry_brain = EntryBrain()
        self.position_brain = PositionBrain()
        self.revival_detector = RevivalDetector()
        self.revival_brain = RevivalBrain()
        self.wallets = WalletTracker()
        self.reflection = ReflectionEngine()

        self.use_sim = settings.use_simulator
        self.feed = SimulatedFeed() if self.use_sim else PumpPortalFeed()

        self.running = False
        self.paused = False
        self._last_price: dict[str, float] = {}
        self._last_liq: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    # --- lookups ---------------------------------------------------------
    def _price(self, mint: str):
        if self.use_sim:
            p = self.feed.price_of(mint)
            if p is not None:
                self._last_price[mint] = p
        return self._last_price.get(mint)

    def _liquidity(self, mint: str):
        return self._last_liq.get(mint, 8_000.0)

    async def _think(self, text: str, **extra) -> None:
        await bus.emit(EventType.THOUGHT, text=text, **extra)

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self.running = True
        mode = "simulator" if self.use_sim else "LIVE pump.fun feed"
        brain = "Claude" if llm.available else "heuristic (no ANTHROPIC_API_KEY)"

        # Resume from disk: cash, fees, and any open positions from before a restart.
        restored = await self.portfolio.load_from_db()

        await bus.emit(
            EventType.STATUS,
            msg=f"Agent online — {settings.trading_mode.value} on {mode}, brain={brain}"
            + (f", resumed {restored} open position(s)" if restored else ""),
            bankroll=settings.starting_bankroll_usd,
        )
        if restored:
            await self._think(
                f"Back online. Picking up {restored} position(s) I still hold and my "
                f"${self.portfolio.cash:.2f} cash. Nothing forgotten.",
            )
        else:
            await self._think(
                f"I'm awake with ${settings.starting_bankroll_usd:.0f} of paper money. I watch "
                f"every launch for {settings.observation_seconds:.0f}s, skip the traps (dev "
                f">{settings.max_dev_holding_pct:.0f}%, sniped ≥{settings.max_sniped_pct:.0f}%), "
                "and only buy coins I actually understand. My exits are my own call."
            )
        self._tasks = [
            asyncio.create_task(self._feed_task()),
            asyncio.create_task(self._launch_task()),
            asyncio.create_task(self._trade_task()),
            asyncio.create_task(self._window_task()),
            asyncio.create_task(self._manage_task()),
        ]
        if settings.scanner_enabled and not self.use_sim:
            self._tasks.append(asyncio.create_task(self._scan_task()))
            self._tasks.append(asyncio.create_task(self._meta_task()))
        # Discovery: study fed/imported traders gradually (budget-guarded).
        self._tasks.append(asyncio.create_task(self._discovery_task()))

    async def stop(self) -> None:
        self.running = False
        await self.feed.stop()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _feed_task(self) -> None:
        try:
            await self.feed.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await bus.emit(EventType.ERROR, where="feed", error=str(e))

    # --- launches: begin observation ------------------------------------
    async def _launch_task(self) -> None:
        try:
            while self.running:
                launch: TokenLaunch = await self.feed.launches.get()
                if self.paused:
                    continue
                await self.wallets.note_creator(launch.creator_wallet)
                self._last_liq[launch.mint] = launch.initial_liquidity_usd
                self.observer.begin(launch)
                await self.feed.watch_trades(launch.mint)
                await bus.emit(
                    EventType.TOKEN_SEEN, symbol=launch.symbol, name=launch.name,
                    mint=launch.mint, description=(launch.description or "")[:140],
                    watching=True,
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await bus.emit(EventType.ERROR, where="launch", error=str(e))

    # --- trades: feed observer + update position marks ------------------
    async def _trade_task(self) -> None:
        try:
            while self.running:
                tick: TradeTick = await self.feed.trades.get()
                self._last_price[tick.mint] = tick.price_usd
                if self.observer.is_watching(tick.mint):
                    self.observer.add_tick(tick)
                pos = self.portfolio.positions.get(tick.mint)
                if pos is not None:
                    pos.last_price = tick.price_usd
                    pos.peak_price = max(pos.peak_price, tick.price_usd)
                    if tick.is_buy:
                        pos.recent_buys += 1
                    else:
                        pos.recent_sells += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await bus.emit(EventType.ERROR, where="trade", error=str(e))

    # --- window closes: decide entry ------------------------------------
    async def _window_task(self) -> None:
        try:
            while self.running:
                await asyncio.sleep(1.0)
                for mint in self.observer.due():
                    # Per-coin guard: one bad evaluation can't kill the whole loop.
                    try:
                        await self._decide_entry(mint)
                    except Exception as e:
                        await bus.emit(EventType.ERROR, where="decide_entry", error=str(e))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await bus.emit(EventType.ERROR, where="window", error=str(e))

    async def _decide_entry(self, mint: str) -> None:
        # Score the buyers we saw so smart-money weight is real, then finalize.
        buyers = self.observer.buyer_wallets(mint)
        smart_scores = await self.wallets.score_early_buyers(buyers)
        obs = self.observer.finalize(mint, smart_scores)
        await self.feed.unwatch_trades(mint)
        if obs is None:
            return

        # Real holder COUNT from Birdeye (useful even on fresh coins). We deliberately do
        # NOT use top-10 concentration on brand-new launches: the bonding curve holds ~all
        # the supply so it always reads ~100%, which is meaningless. Early concentration is
        # already covered by the sniped/bundled check. Top-10 is only meaningful for old
        # coins, so it's applied on the revival path instead.
        from .ingestion.birdeye import birdeye
        obs.top10_holding_pct = -1.0  # not evaluated for fresh launches
        if birdeye.available:
            ov = await birdeye.overview(mint)
            if ov and ov.get("holders"):
                obs.holders = int(ov["holders"])

        # 1. Safety gate.
        safety = checks.evaluate(obs)
        if not safety.passed:
            await self._think(f"Skip {obs.launch.symbol}: {', '.join(safety.reasons)}.",
                              symbol=obs.launch.symbol, mint=mint, decision="SKIP")
            await bus.emit(EventType.DECISION, symbol=obs.launch.symbol, mint=mint, action="SKIP",
                           reason="; ".join(safety.reasons))
            await repo.upsert_watch(
                mint, symbol=obs.launch.symbol, name=obs.launch.name, source="launch",
                disposition="skipped", mcap=obs.market_cap_usd,
                skip_reason="; ".join(safety.reasons),
            )
            return

        # 1b. Cheap pre-filter (no LLM). NOTE: PumpPortal's free tier reliably delivers the
        # creation event but NOT most follow-on trade ticks, so "buys seen" undercounts
        # badly. We therefore do NOT skip on low tick counts. We only skip the clearest
        # junk: a launch with NO name/symbol/description and no socials at all (pure noise).
        # Everything with any identity goes to the thesis brain, which judges it properly.
        l = obs.launch
        has_identity = bool((l.name and l.name != "Unknown") or l.description or l.twitter or l.website)
        if not has_identity:
            await bus.emit(EventType.DECISION, symbol=l.symbol, mint=mint, action="SKIP",
                           reason="no identity at all (no name/desc/socials)")
            await repo.upsert_watch(
                mint, symbol=l.symbol, name=l.name, source="launch",
                disposition="skipped", mcap=obs.market_cap_usd,
                skip_reason="no identity at all (no name/desc/socials)",
            )
            return

        # 2. Understand what it is.
        thesis = await self.understanding.understand(obs)
        await self._think(
            f"{obs.launch.symbol} looks like [{thesis.category}]: {thesis.summary} "
            f"(narrative {thesis.narrative_strength:.2f}, virality {thesis.virality:.2f})",
            symbol=obs.launch.symbol, mint=mint,
        )

        # 3. Entry decision. Feed lessons + advice the agent chose to adopt.
        playbook = (await repo.get_playbook()).content
        lessons = [l.text for l in await repo.recent_lessons(8)]
        lessons += [f"(adopted advice) {a.adopted_rule}" for a in await repo.active_advice(8)]
        # Taught context: remembered as background patterns, not rules.
        lessons += [
            f"(context, weigh yourself) {k.takeaway or k.text}"
            for k in await repo.recent_market_knowledge(6)
        ]
        # The live meta: what lores/animals/themes are running right now, so a fresh coin
        # riffing on a hot meta gets recognized as potentially worthy.
        meta_ctx = meta_tracker.context_for_brain()
        if meta_ctx:
            lessons.insert(0, meta_ctx)
        decision = await self.entry_brain.decide(obs, thesis, playbook, lessons)
        await bus.emit(
            EventType.DECISION, symbol=obs.launch.symbol, mint=mint, action=decision.action.value,
            conviction=round(decision.conviction, 2), size_pct=decision.size_pct,
            reason=decision.rationale, thesis=thesis.summary, category=thesis.category,
        )
        await self._think(decision.rationale, symbol=obs.launch.symbol, mint=mint,
                          decision=decision.action.value)
        if decision.action != Action.BUY:
            # Remember it — a coin we passed on may revive later, and we learn from misses.
            await repo.upsert_watch(
                mint, symbol=obs.launch.symbol, name=obs.launch.name, source="launch",
                disposition="skipped", mcap=obs.market_cap_usd,
                thesis_category=thesis.category, thesis=thesis.summary,
                skip_reason=decision.rationale,
            )
            return

        # 4. Risk bounds.
        equity = self.portfolio.equity(self._price)
        verdict = self.risk.check(decision.size_pct, self.portfolio.cash, equity,
                                  self.portfolio.open_count())
        if not verdict.approved:
            await self._think(f"Risk blocked {obs.launch.symbol}: {verdict.reason}.",
                              symbol=obs.launch.symbol, decision="RISK_BLOCK")
            return

        # 5. Paper buy. Use a REAL market price for entry (DexScreener/Birdeye) so entry and
        # future exit prices are on the same scale. The launch's derived price can be off,
        # which would produce nonsense PnL. Fall back to the observed price if unavailable.
        real = await scanner.snapshot(mint, pumpfun_only=False)
        entry_priced = bool(real and real.price_usd > 0)
        if entry_priced:
            obs.price_now = real.price_usd
            if real.liquidity_usd:
                obs.liquidity_usd = real.liquidity_usd
            self._last_price[mint] = real.price_usd
        pos = await self.portfolio.open_position(
            obs, verdict.size_usd, decision.conviction, thesis, decision.rationale, decision.plan,
        )
        if pos is not None:
            # priced only if we anchored entry to a real market price; if not, the
            # untracked-exit timeout will clean it up rather than showing nonsense PnL.
            pos.priced = entry_priced
            await repo.upsert_watch(
                mint, symbol=obs.launch.symbol, name=obs.launch.name, source="launch",
                disposition="holding", mcap=obs.market_cap_usd,
                thesis_category=thesis.category, thesis=thesis.summary,
            )
            await self._think(
                f"Bought {obs.launch.symbol} — ${verdict.size_usd:.2f} "
                f"(conviction {decision.conviction:.0%}). Plan: {decision.plan}",
                symbol=obs.launch.symbol, decision="BUY",
            )
            # Share conviction plays publicly (only the ones it actually believes in).
            if settings.x_post_buys and decision.conviction >= 0.6:
                await x_poster.maybe_post(
                    "conviction_buy",
                    f"Bought ${obs.launch.symbol} — [{thesis.category}] {thesis.summary}. "
                    f"Conviction {decision.conviction:.0%}. Why: {decision.rationale}",
                )

    # --- market scanner: OLD coin revivals ------------------------------
    async def _scan_task(self) -> None:
        """Slow sweep of the market to catch old coins waking back up."""
        await asyncio.sleep(15)  # let the launch feed warm up first
        try:
            while self.running:
                if not self.paused:
                    try:
                        await self._scan_once()
                    except Exception as e:
                        await bus.emit(EventType.ERROR, where="scan", error=str(e))
                await asyncio.sleep(settings.scanner_interval_seconds)
        except asyncio.CancelledError:
            pass

    async def _meta_task(self) -> None:
        """Learn the current meta from top runners so fresh coins in a hot lore get noticed."""
        await asyncio.sleep(8)
        try:
            while self.running:
                try:
                    await meta_tracker.refresh()
                    if meta_tracker.metas:
                        await self._think(
                            f"📊 current meta read: {', '.join(meta_tracker.metas[:6])}. "
                            f"{meta_tracker.summary}"
                        )
                except Exception as e:
                    await bus.emit(EventType.ERROR, where="meta", error=str(e))
                await asyncio.sleep(settings.meta_refresh_seconds)
        except asyncio.CancelledError:
            pass

    async def _discovery_task(self) -> None:
        """Study pending tracked traders in small batches, budget-guarded, so the agent
        forms opinions on smart-money wallets without ever blowing the Helius credit."""
        from .brain.identity import trader_judge
        from .ingestion.helius_budget import helius_budget

        await asyncio.sleep(30)
        try:
            while self.running:
                if not self.paused and helius_budget.remaining_today > 100:
                    pending = await repo.pending_traders(limit=settings.wallet_study_batch)
                    for t in pending:
                        if not self.running:
                            break
                        try:
                            await trader_judge.judge(t.id, t.x_handle, t.wallet, t.label)
                        except Exception as e:
                            await bus.emit(EventType.ERROR, where="discovery", error=str(e))
                        await asyncio.sleep(3)  # gentle spacing between wallets
                    if pending:
                        await bus.emit(
                            EventType.THOUGHT,
                            text=f"studied {len(pending)} more wallets. "
                            f"helius calls left today: {helius_budget.remaining_today}",
                        )
                await asyncio.sleep(settings.wallet_study_interval_seconds)
        except asyncio.CancelledError:
            pass

    async def _scan_once(self) -> None:
        # Candidates = trending pump.fun coins + our own watchlist (memory).
        trending = await scanner.trending_pumpfun(limit=30)
        watch = [w.mint for w in await repo.watchlist_for_revival(limit=40)]
        seen: set[str] = set()
        checked = 0
        for mint in trending + watch:
            if mint in seen or mint in self.portfolio.positions:
                continue
            seen.add(mint)
            if checked >= settings.scanner_max_checks_per_sweep:
                break
            coin = await scanner.snapshot(mint)
            if coin is None:
                continue
            checked += 1
            await repo.mark_revival_check(mint, coin.market_cap_usd)
            signal = self.revival_detector.detect(coin)
            # Track big moves on coins we skipped so we learn from the ones that got away.
            await self._note_watch_move(coin)
            if signal.waking:
                await self._decide_revival(coin, signal)

    async def _note_watch_move(self, coin) -> None:
        w = await repo.get_watch(coin.mint)
        if w is None:
            return
        # If a coin we skipped has multiplied since we saw it, flag it and (the first time)
        # run a post-mortem so the agent learns what it missed, on its own.
        if w.disposition == "skipped" and w.mcap_at_seen > 0:
            if coin.market_cap_usd >= w.mcap_at_seen * settings.missed_runner_multiple:
                is_new = await repo.flag_missed_runner(coin.mint)
                if is_new and not w.postmortem_done:
                    from .learning.miss_analyst import miss_analyst
                    await miss_analyst.analyze(coin, w)

    async def _decide_revival(self, coin, signal) -> None:
        await self._think(
            f"👀 {coin.symbol} (old, {coin.age_hours:.0f}h) may be reviving: "
            f"{signal.pattern} — {'; '.join(signal.reasons[:2])}",
            symbol=coin.symbol,
        )
        thesis = await self.understanding.understand_coin(coin)
        playbook = (await repo.get_playbook()).content
        lessons = [l.text for l in await repo.recent_lessons(6)]
        lessons += [f"(adopted advice) {a.adopted_rule}" for a in await repo.active_advice(6)]
        decision = await self.revival_brain.decide(coin, signal, thesis, playbook, lessons)
        await bus.emit(
            EventType.DECISION, symbol=coin.symbol, action=decision.action.value,
            conviction=round(decision.conviction, 2), size_pct=decision.size_pct,
            reason=decision.rationale, thesis=thesis.summary, category=thesis.category,
            source="revival",
        )
        await self._think(decision.rationale, symbol=coin.symbol, decision=decision.action.value)
        if decision.action != Action.BUY:
            await repo.upsert_watch(
                coin.mint, symbol=coin.symbol, name=coin.name, source="revival",
                disposition="skipped", mcap=coin.market_cap_usd,
                thesis_category=thesis.category, thesis=thesis.summary,
            )
            return
        equity = self.portfolio.equity(self._price)
        verdict = self.risk.check(decision.size_pct, self.portfolio.cash, equity,
                                  self.portfolio.open_count())
        if not verdict.approved:
            await self._think(f"Risk blocked revival {coin.symbol}: {verdict.reason}.",
                              symbol=coin.symbol, decision="RISK_BLOCK")
            return
        self._last_price[coin.mint] = coin.price_usd
        self._last_liq[coin.mint] = coin.liquidity_usd
        pos = await self.portfolio.open_from_market(
            coin, verdict.size_usd, decision.conviction, thesis, decision.rationale, decision.plan,
        )
        if pos is not None:
            await repo.upsert_watch(
                coin.mint, symbol=coin.symbol, name=coin.name, source="revival",
                disposition="holding", mcap=coin.market_cap_usd,
                thesis_category=thesis.category, thesis=thesis.summary,
            )
            await self._think(
                f"🔁 Revival buy {coin.symbol} — ${verdict.size_usd:.2f} "
                f"(conviction {decision.conviction:.0%}). Plan: {decision.plan}",
                symbol=coin.symbol, decision="BUY",
            )
            if settings.x_post_buys and decision.conviction >= 0.6:
                await x_poster.maybe_post(
                    "revival_buy",
                    f"Old coin ${coin.symbol} looks like it's waking back up ({coin.age_hours:.0f}h old). "
                    f"Took a position. {decision.rationale}",
                )

    # --- manage open positions ------------------------------------------
    async def _manage_task(self) -> None:
        tick = 0
        try:
            while self.running:
                await asyncio.sleep(3)
                # Each step guarded so a single failure never stops position management.
                try:
                    if tick % 5 == 0:
                        await self._refresh_revival_prices()
                    for mint in list(self.portfolio.positions.keys()):
                        try:
                            await self._manage_one(mint)
                        except Exception as e:
                            await bus.emit(EventType.ERROR, where="manage_one", error=str(e))
                    await self.reflection.maybe_reflect()
                    tick += 1
                    if tick % 2 == 0:
                        await self._publish_status()
                except Exception as e:
                    await bus.emit(EventType.ERROR, where="manage_cycle", error=str(e))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await bus.emit(EventType.ERROR, where="manage", error=str(e))

    async def _refresh_revival_prices(self) -> None:
        """Revival positions aren't on the trade websocket, so poll their price."""
        # Poll price for EVERY open position. Launch positions barely get WS trade ticks
        # on the free tier, so without this their price would be stuck at entry forever and
        # they'd never resolve. DexScreener/Birdeye give a reliable current price.
        for mint, pos in list(self.portfolio.positions.items()):
            coin = await scanner.snapshot(mint, pumpfun_only=False)
            price = coin.price_usd if coin and coin.price_usd > 0 else None
            if price is None:
                be = await birdeye.overview(mint) if birdeye.available else None
                if be and be.get("price"):
                    price = float(be["price"])
            if price:
                self._last_price[mint] = price
                if coin and coin.liquidity_usd:
                    self._last_liq[mint] = coin.liquidity_usd
                pos.last_price = price
                pos.peak_price = max(pos.peak_price, price)
                pos.priced = True

    async def _manage_one(self, mint: str) -> None:
        pos = self.portfolio.positions.get(mint)
        if pos is None:
            return
        price = self._price(mint) or pos.last_price or pos.entry_price
        pos.peak_price = max(pos.peak_price, price)
        mult = pos.multiple(price)
        peak_mult = pos.peak_multiple()
        dd = 1.0 - (price / pos.peak_price) if pos.peak_price else 0.0
        held_min = (time.time() - pos.opened_ts) / 60.0
        net = pos.recent_buys - pos.recent_sells

        # --- HARD STOP (survival floor, runs before the brain) ---
        # The agent may cut earlier at its discretion, but it can never hold past these.
        if settings.hard_stop_enabled:
            hard_reason = None
            if mult <= (1.0 - settings.hard_stop_loss_pct):
                hard_reason = f"HARD STOP: down {(1-mult)*100:.0f}% from entry"
            elif (
                peak_mult >= settings.hard_stop_min_peak_x
                and dd >= settings.hard_trailing_giveback_pct
            ):
                hard_reason = (
                    f"HARD TRAILING STOP: gave back {dd*100:.0f}% from peak {peak_mult:.1f}x"
                )
            if hard_reason:
                await self._think(
                    f"🛑 {pos.symbol}: {hard_reason} — forcing exit to protect capital.",
                    symbol=pos.symbol, decision="SELL")
                result = await self.portfolio.sell(mint, price, self._liquidity(mint), hard_reason)
                if result is not None:
                    _cp, won = result
                    if pos.obs is not None:
                        await self.wallets.resolve_token(pos.obs, won)
                return

        # --- Time-based cleanup so nothing is held forever ---
        # 1) Never got a live price (untrackable) -> exit fast, it's dead weight.
        # 2) Held a while and basically flat -> nothing happened, free the capital.
        timeout_reason = None
        if not getattr(pos, "priced", False) and held_min >= settings.untracked_exit_minutes:
            timeout_reason = f"no price data after {held_min:.0f}m, exiting untrackable position"
        elif held_min >= settings.max_hold_minutes and abs(mult - 1.0) <= settings.flat_band_pct:
            timeout_reason = f"held {held_min:.0f}m and went nowhere ({mult:.2f}x), cutting dead money"
        if timeout_reason:
            await self._think(f"⏲ {pos.symbol}: {timeout_reason}.", symbol=pos.symbol, decision="SELL")
            result = await self.portfolio.sell(mint, price, self._liquidity(mint), timeout_reason)
            if result is not None:
                _cp, won = result
                if pos.obs is not None:
                    await self.wallets.resolve_token(pos.obs, won)
            return

        state = {
            "symbol": pos.symbol, "entry_mcap": pos.entry_mcap,
            "now_mcap": pos.entry_mcap * mult, "multiple": mult, "peak_multiple": peak_mult,
            "dd_from_peak": dd, "held_min": held_min,
            "frac_remaining": pos.tokens_remaining / pos.tokens_initial if pos.tokens_initial else 0,
            "recent_buys": pos.recent_buys, "recent_sells": pos.recent_sells, "net_flow": net,
            "unreal_pct": (mult - 1.0) * 100, "plan": pos.plan,
            "source": getattr(pos, "source", "launch"),
        }
        decision = await self.position_brain.manage(state)
        # Reset flow counters each evaluation window.
        pos.recent_buys = 0
        pos.recent_sells = 0

        liq = self._liquidity(mint)
        result = None
        if decision.action == ManageAction.HOLD:
            return
        elif decision.action == ManageAction.TRIM:
            await self._think(
                f"Trimming {decision.trim_fraction:.0%} of {pos.symbol} at {mult:.2f}x — {decision.reason}",
                symbol=pos.symbol, decision="TRIM")
            result = await self.portfolio.trim(mint, decision.trim_fraction, price, liq, decision.reason)
        elif decision.action == ManageAction.SELL:
            await self._think(
                f"Selling {pos.symbol} at {mult:.2f}x (peak {peak_mult:.2f}x) — {decision.reason}",
                symbol=pos.symbol, decision="SELL")
            result = await self.portfolio.sell(mint, price, liq, decision.reason)
        elif decision.action == ManageAction.ADD:
            await self._think(f"Holding/adding conviction on {pos.symbol} — {decision.reason}",
                              symbol=pos.symbol, decision="HOLD")

        if result is not None:
            closed_pos, won = result
            if pos.obs is not None:
                await self.wallets.resolve_token(pos.obs, won)
# Post notable exits — big wins AND honest mistakes. A real 50x+ absolutely
            # should be shared. The only thing we block is a number that came from BAD DATA:
            # a position never anchored to a real market price (pos.priced=False) can't have a
            # trustworthy multiple. Data validity is the gate, not the size of the win.
            if settings.x_post_closes:
                pnl_x = pos.peak_multiple()
                final_mult = mult
                data_ok = getattr(pos, "priced", False) and final_mult > 0 and pnl_x > 0
                notable = data_ok and (final_mult >= 1.8 or final_mult <= 0.6)
                if notable:
                    if final_mult >= 10:
                        outcome = "absolute banger, this is what we live for"
                    elif won:
                        outcome = "banked a winner"
                    else:
                        outcome = "took a loss"
                    await x_poster.maybe_post(
                        "trade_close",
                        f"Closed ${pos.symbol} at {final_mult:.2f}x (peaked {pnl_x:.2f}x) — {outcome}. "
                        f"Reason: {decision.reason}. What I'm taking from it.",
                    )

    async def _publish_status(self) -> None:
        equity = self.portfolio.equity(self._price)
        pnl = equity - self.portfolio.starting_cash
        await bus.emit(
            EventType.STATUS,
            equity=round(equity, 2), cash=round(self.portfolio.cash, 2),
            total_pnl=round(pnl, 2),
            total_pnl_pct=round(pnl / self.portfolio.starting_cash * 100, 2),
            fees_paid=round(self.portfolio.total_fees, 2),
            open_positions=self.portfolio.open_count(),
            kill_switch=self.risk.kill_switch,
            positions=[
                {
                    "symbol": p.symbol, "mint": p.mint,
                    "multiple": round(p.multiple(self._price(p.mint) or p.last_price or p.entry_price), 2),
                    "peak_x": round(p.peak_multiple(), 2),
                    "value_usd": round((self._price(p.mint) or p.last_price or p.entry_price) * p.tokens_remaining, 2),
                    "plan": p.plan[:80],
                }
                for p in self.portfolio.positions.values()
            ],
        )


agent = Agent()
