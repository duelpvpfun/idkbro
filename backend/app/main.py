"""FastAPI entrypoint: serves the dashboard, streams agent events over WebSocket,
and exposes REST endpoints for control + injecting your own knowledge notes.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import agent
from .backup import backup_loop, make_snapshot
from .brain.advice import advice_evaluator, knowledge_processor
from .brain.identity import identity_brain, trader_judge
from .brain.meta import meta_tracker
from .brain.llm import llm
from .config import settings
from .db import repository as repo
from .db.database import init_db
from .events import bus
from .ingestion.birdeye import birdeye
from .ingestion.helius_budget import helius_budget
from .social.image_gen import image_gen
from .social.x_poster import x_poster

_FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(agent.start())
    asyncio.create_task(backup_loop())     # periodic memory snapshots
    yield
    await agent.stop()


app = FastAPI(title="idkbro — Solana Memecoin Trader Agent", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    # Replay recent history so a fresh page isn't blank.
    await websocket.send_json({"type": "history", "ts": 0, "data": {"events": bus.history()}})
    try:
        async for event in bus.subscribe():
            await websocket.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.get("/api/status")
async def status() -> JSONResponse:
    equity = agent.portfolio.equity(agent._price)
    start = agent.portfolio.starting_cash
    return JSONResponse(
        {
            "mode": settings.trading_mode.value,
            "using_simulator": agent.use_sim,
            "brain": "Claude" if llm.available else "heuristic",
            "paused": agent.paused,
            "kill_switch": agent.risk.kill_switch,
            "starting_bankroll": start,
            "equity": round(equity, 2),
            "cash": round(agent.portfolio.cash, 2),
            "total_pnl": round(equity - start, 2),
            "total_pnl_pct": round((equity - start) / start * 100, 2) if start else 0,
            "fees_paid": round(agent.portfolio.total_fees, 2),
            "open_positions": agent.portfolio.open_count(),
            "x_enabled": settings.x_enabled,
            "x_dry_run": settings.x_dry_run,
            "scanner_enabled": settings.scanner_enabled and not agent.use_sim,
            "helius_used_today": helius_budget.used_today,
            "helius_cap_today": settings.helius_max_calls_per_day,
            "llm_used_today": llm.used_today,
            "llm_cap_today": settings.llm_max_calls_per_day,
            "birdeye_used_today": birdeye.budget.used,
            "birdeye_cap_today": settings.birdeye_max_calls_per_day,
            "birdeye_on": birdeye.available,
            "x_budget": x_poster.budget_status(),
            "metas": meta_tracker.metas,
            "meta_summary": meta_tracker.summary,
        }
    )


@app.post("/api/backup")
async def backup_now() -> JSONResponse:
    """Snapshot the agent's memory on demand."""
    import asyncio

    path = await asyncio.to_thread(make_snapshot)
    return JSONResponse({"ok": bool(path), "snapshot": os.path.basename(path) if path else None})


@app.get("/api/trades")
async def trades() -> JSONResponse:
    rows = await repo.recent_trades(limit=100)
    return JSONResponse(
        [
            {
                "id": t.id,
                "symbol": t.symbol,
                "status": t.status,
                "category": t.thesis_category,
                "thesis": t.thesis,
                "conviction": round(t.conviction, 2),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "size_usd": round(t.size_usd, 2),
                "peak_x": round(t.peak_multiple, 2),
                "pnl_usd": round(t.pnl_usd, 2),
                "pnl_pct": round(t.pnl_pct * 100, 1),
                "exit_reason": t.exit_reason,
                "rationale": t.rationale,
                "plan": t.plan,
            }
            for t in rows
        ]
    )


@app.get("/api/wallets")
async def wallets() -> JSONResponse:
    rows = await repo.good_wallets(limit=30, min_score=0.0)
    return JSONResponse(
        [
            {
                "wallet": w.wallet,
                "role": w.role,
                "score": w.score,
                "early_hits": w.early_hits,
                "early_misses": w.early_misses,
                "tokens_created": w.tokens_created,
            }
            for w in rows
        ]
    )


@app.get("/api/lessons")
async def lessons() -> JSONResponse:
    rows = await repo.recent_lessons(limit=25)
    return JSONResponse([{"text": l.text, "category": l.category} for l in rows])


@app.get("/api/missed")
async def missed() -> JSONResponse:
    """Coins the agent skipped that later ran — 'the ones that got away'."""
    rows = await repo.recent_missed_runners(limit=15)
    return JSONResponse(
        [
            {
                "symbol": m.symbol,
                "category": m.thesis_category,
                "seen_mcap": round(m.mcap_at_seen),
                "peak_mcap": round(m.peak_mcap_seen),
                "x": round(m.peak_mcap_seen / m.mcap_at_seen, 1) if m.mcap_at_seen else 0,
            }
            for m in rows
        ]
    )


@app.get("/api/playbook")
async def playbook() -> JSONResponse:
    pb = await repo.get_playbook()
    return JSONResponse({"version": pb.version, "content": pb.content, "updated_ts": pb.updated_ts})


class Advice(BaseModel):
    text: str
    mint: str = ""


@app.post("/api/advice")
async def add_advice(note: Advice) -> JSONResponse:
    """Give the agent advice. It evaluates it critically and decides whether to adopt,
    partially adopt, or reject it — it is never forced to obey."""
    saved = await repo.add_advice(note.text, note.mint)
    # Evaluate in the background so the request returns immediately.
    asyncio.create_task(advice_evaluator.evaluate(saved.id, note.text))
    return JSONResponse({"ok": True, "id": saved.id})


class Knowledge(BaseModel):
    text: str
    topic: str = ""


@app.post("/api/knowledge")
async def add_knowledge(note: Knowledge) -> JSONResponse:
    """Teach the agent WHY something happened (e.g. 'X ran because a KOL quoted it').
    Stored as remembered context it pattern-matches against later, NOT a rule it obeys."""
    saved = await repo.add_market_knowledge(note.text, topic=note.topic)
    asyncio.create_task(knowledge_processor.process(saved.id, note.text, note.topic))
    return JSONResponse({"ok": True, "id": saved.id})


@app.get("/api/knowledge")
async def list_knowledge() -> JSONResponse:
    rows = await repo.recent_market_knowledge(limit=30)
    return JSONResponse(
        [{"topic": k.topic, "text": k.text, "takeaway": k.takeaway} for k in rows]
    )


class StudyCA(BaseModel):
    ca: str
    note: str = ""


@app.post("/api/study")
async def study_ca(body: StudyCA) -> JSONResponse:
    """Just paste a CA. The agent checks its own history with the coin, pulls how it
    performed, and works out the lesson itself. Scales without you explaining each one."""
    result = await knowledge_processor.study_ca(body.ca.strip(), body.note.strip())
    return JSONResponse({"ok": True, **result})


class Signal(BaseModel):
    wallet: str = ""
    x_handle: str = ""
    label: str = ""          # who they are
    coin: str = ""           # mint or ticker they aped
    thesis: str = ""         # written thesis, if any


@app.post("/api/signal")
async def ingest_signal(sig: Signal) -> JSONResponse:
    """Generic intake for external FOMO/thesis tools: 'wallet W aped coin C, thesis T'.

    Any script you run can POST here. The trader becomes a discovery candidate the agent
    judges on its own; the thesis is stored as remembered context (not a rule). This is the
    hook to wire any KOL/FOMO data source without the agent blindly following it."""
    added_trader = False
    if sig.wallet or sig.x_handle:
        t = await repo.add_tracked_trader(sig.x_handle, sig.wallet, sig.label or "signal", source="signal")
        asyncio.create_task(trader_judge.judge(t.id, sig.x_handle, sig.wallet, sig.label))
        added_trader = True
    if sig.thesis:
        topic = sig.coin or sig.x_handle or sig.wallet[:8]
        note = await repo.add_market_knowledge(sig.thesis, topic=topic, category="signal")
        asyncio.create_task(knowledge_processor.process(note.id, sig.thesis, topic))
    return JSONResponse({"ok": True, "trader_added": added_trader, "thesis_stored": bool(sig.thesis)})


@app.get("/api/advice")
async def list_advice() -> JSONResponse:
    rows = await repo.recent_advice(limit=40)
    return JSONResponse(
        [
            {
                "id": a.id,
                "text": a.text,
                "stance": a.stance,
                "reasoning": a.reasoning,
                "adopted_rule": a.adopted_rule,
                "active": bool(a.active),
            }
            for a in rows
        ]
    )


# ---------------------------------------------------------------- identity
@app.get("/api/identity")
async def get_identity() -> JSONResponse:
    ident = await repo.get_identity()
    return JSONResponse(
        {
            "chosen": bool(ident.chosen),
            "display_name": ident.display_name,
            "bio": ident.bio,
            "pfp_concept": ident.pfp_concept,
            "banner_concept": ident.banner_concept,
            "applied_to_x": bool(ident.applied_to_x),
        }
    )


@app.post("/api/identity/create")
async def create_identity(post_intro: bool = True) -> JSONResponse:
    """Ask the agent to invent its own identity, apply name+bio to X, generate + set its
    pfp/banner if an image API is available, and post an intro."""
    data = await identity_brain.choose_identity()
    if not data:
        return JSONResponse({"ok": False, "error": "no LLM available to choose identity"})
    ok, why = await x_poster.update_profile(data["display_name"], data["bio"])
    if ok and not settings.x_dry_run:
        await repo.mark_identity_applied()

    # If an image API is configured, the agent makes + sets its own pfp/banner.
    images = {"pfp": None, "banner": None}
    if image_gen.available:
        pfp = await image_gen.make_pfp(data.get("pfp_concept", ""))
        if pfp:
            await x_poster.set_pfp(pfp)
            images["pfp"] = os.path.basename(pfp)
        banner = await image_gen.make_banner(data.get("banner_concept", ""))
        if banner:
            await x_poster.set_banner(banner)
            images["banner"] = os.path.basename(banner)

    if post_intro and data.get("intro_tweet"):
        await x_poster.maybe_post("intro", data["intro_tweet"])
    return JSONResponse({
        "ok": True, "identity": data, "profile_update": why,
        "images_generated": images, "image_api": image_gen.available,
    })


# ---------------------------------------------------------------- tracked traders
class TraderIn(BaseModel):
    x_handle: str = ""
    wallet: str = ""
    label: str = ""


@app.post("/api/traders")
async def add_trader(t: TraderIn) -> JSONResponse:
    """Feed the agent a pump.fun trader (real X handle + wallet). It decides on its own
    whether to trust/watch/ignore. We never force it to follow anyone."""
    if not t.x_handle and not t.wallet:
        return JSONResponse({"ok": False, "error": "need at least an x_handle or wallet"})
    saved = await repo.add_tracked_trader(t.x_handle, t.wallet, t.label, source="user")
    asyncio.create_task(trader_judge.judge(saved.id, t.x_handle, t.wallet, t.label))
    return JSONResponse({"ok": True, "id": saved.id})


@app.post("/api/traders/bulk")
async def add_traders_bulk(items: list[TraderIn]) -> JSONResponse:
    """Feed many traders at once. Each gets judged independently by the agent."""
    ids = []
    for t in items:
        if not t.x_handle and not t.wallet:
            continue
        saved = await repo.add_tracked_trader(t.x_handle, t.wallet, t.label, source="user")
        asyncio.create_task(trader_judge.judge(saved.id, t.x_handle, t.wallet, t.label))
        ids.append(saved.id)
    return JSONResponse({"ok": True, "added": len(ids)})


class TeachTrader(BaseModel):
    x_handle: str = ""
    wallet: str = ""
    style: str          # your explanation of their trading style / who they are


@app.post("/api/traders/teach")
async def teach_trader(t: TeachTrader) -> JSONResponse:
    """Explain a trader's style so the agent understands them (e.g. 'Ga__ke is a legend who
    shitposts but snipes early narratives'). Stored + he re-judges them WITH your context."""
    existing = await repo.find_trader(t.x_handle, t.wallet)
    if existing is None:
        existing = await repo.add_tracked_trader(t.x_handle, t.wallet, t.style, source="user")
    # Store the style AND backfill the wallet if the record was handle-only.
    await repo.set_trader_style(existing.id, t.style, wallet=t.wallet)
    wallet = existing.wallet or t.wallet
    handle = existing.x_handle or t.x_handle
    combined_label = f"{existing.label} | mentor says: {t.style}".strip(" |")
    asyncio.create_task(trader_judge.judge(existing.id, handle, wallet, combined_label))
    return JSONResponse({"ok": True, "id": existing.id})


@app.get("/api/traders")
async def list_traders() -> JSONResponse:
    rows = await repo.all_traders(limit=200)
    return JSONResponse(
        [
            {
                "id": t.id,
                "x_handle": t.x_handle,
                "wallet": t.wallet,
                "label": t.label,
                "stance": t.stance,
                "reasoning": t.reasoning,
                "followed_on_x": bool(t.followed_on_x),
            }
            for t in rows
        ]
    )


class Control(BaseModel):
    action: str  # pause | resume | kill | revive


@app.post("/api/control")
async def control(cmd: Control) -> JSONResponse:
    if cmd.action == "pause":
        agent.paused = True
    elif cmd.action == "resume":
        agent.paused = False
    elif cmd.action == "kill":
        agent.risk.kill_switch = True
    elif cmd.action == "revive":
        agent.risk.kill_switch = False
    return JSONResponse({"ok": True, "paused": agent.paused, "kill_switch": agent.risk.kill_switch})


class TweetDelete(BaseModel):
    tweet_id: str = ""
    last: int = 0        # delete the last N tweets instead of a specific id


@app.post("/api/tweet/delete")
async def delete_tweet(body: TweetDelete) -> JSONResponse:
    """Delete a bad tweet by id, or the last N the agent posted."""
    if body.tweet_id:
        ok = await x_poster.delete_tweet(body.tweet_id)
        return JSONResponse({"ok": ok})
    if body.last > 0:
        n = await x_poster.delete_last(body.last)
        return JSONResponse({"ok": True, "deleted": n})
    return JSONResponse({"ok": False, "error": "need tweet_id or last"})


if os.path.isdir(_FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


def run() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
