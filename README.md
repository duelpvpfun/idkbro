# idkbro — Autonomous Solana Memecoin Trader Agent

An AI agent that listens to pump.fun launches, reads social/market context, decides
what to buy, how long to hold, and **learns from its own trades**. It paper-trades
first and only graduates to real money after it proves profitable.

> ⚠️ **This is experimental software for research/education.** Memecoin trading is
> extremely high risk — the vast majority of tokens go to zero. Never run this with
> money you can't afford to lose. Paper mode is the default and the safe default.

## What it does

1. **Ingests** new + existing pump.fun tokens and their market data (live via
   PumpPortal/Helius, or a built-in **simulator** when you have no API keys).
2. **Checks safety** — anti-rug / authority / liquidity heuristics that can veto a buy.
3. **Reads context** — social + linked sites (stubbed now, pluggable later).
4. **Decides** using a rules engine plus an optional **Claude** brain, producing a
   written rationale, a position size, and an exit plan (targets + stop + max hold).
5. **Risk-manages** every trade (tiny sizing, daily loss cap, kill switch).
6. **Executes** in a realistic **paper broker** (fees + slippage modeled).
7. **Manages positions** and closes on targets/stops/timeouts.
8. **Learns** — journals every trade and periodically reflects to extract lessons and
   build a "good wallets" list.
9. **Streams its thoughts** to a live web dashboard over WebSocket.

## Quick start

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional: add HELIUS/ANTHROPIC keys
python -m app.main            # starts API + dashboard on http://localhost:8000
```

Open http://localhost:8000 to watch the agent think and trade.

**No API keys?** It just works — the agent runs against a built-in market simulator so
you can develop and watch the full loop end-to-end offline.

## Architecture

```
Ingestion → Context → Memory(DB) → Decision(rules + Claude) → Risk(veto) →
Execution(paper) → Positions → Learning(journal + reflection) → back to Memory
```

See `backend/app/` for each layer. `docs/` (coming) will hold deeper notes.

## Safety model

- Default mode is **PAPER**. Live mode requires an explicit env flag *and* a funded
  wallet you configure yourself.
- The **RiskManager** is a separate layer that can veto the brain: max % per trade,
  max concurrent positions, daily loss limit, mandatory stop-loss + max hold time.
- A **kill switch** halts all new entries.

## Roadmap

- [x] Paper-trading MVP (this scaffold)
- [ ] Real PumpPortal/Helius wiring hardening
- [ ] Real social reader (X API + site fetch → structured signals)
- [ ] Vector memory for "this reminds me of…" recall
- [ ] Live Jupiter execution behind the risk gate
- [ ] Backtesting on historical tokens
