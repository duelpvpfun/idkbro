"""Load a list of REAL pump.fun traders into the agent for it to judge.

Edit backend/data/seed_traders.json with real X handles + wallets (see that file), then:
    source .venv/bin/activate && python -m tools.seed_traders

The agent then decides trust/watch/ignore for each on its own. We never auto-trust anyone.
This script does NOT invent data; it only loads what you put in the JSON.
"""

from __future__ import annotations

import asyncio
import json
import os

SEED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "seed_traders.json")


async def main() -> None:
    from app.db.database import init_db
    from app.db import repository as repo
    from app.brain.identity import trader_judge

    await init_db()
    if not os.path.exists(SEED):
        print(f"No seed file at {SEED}")
        return
    with open(SEED, "r", encoding="utf-8") as f:
        rows = json.load(f)

    added = 0
    for r in rows:
        handle = str(r.get("x_handle", "")).strip()
        wallet = str(r.get("wallet", "")).strip()
        label = str(r.get("label", "")).strip()
        if not handle and not wallet:
            continue  # skip comment/empty rows
        t = await repo.add_tracked_trader(handle, wallet, label, source="seed")
        await trader_judge.judge(t.id, handle, wallet, label)
        added += 1
        print(f"fed @{handle or wallet[:8]} -> agent is judging it")

    print(f"\nDone. Fed {added} traders. Check /api/traders for the agent's verdicts.")


if __name__ == "__main__":
    asyncio.run(main())
