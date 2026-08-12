"""Import a tracked-wallet list (e.g. Cupsey's list) into the agent as candidates.

Loads wallets from a JSON file (default: repo root 'Wallet List.txt', the format exported
by trackers: a list of {trackedWalletAddress, name, ...}). Each becomes a TrackedTrader with
stance 'pending'. It does NOT study them here (that would burn Helius credit in a burst) —
the agent's throttled discovery loop studies them over time, budget-guarded.

Run:  source .venv/bin/activate && python -m tools.import_wallets ["/path/to/list.json"]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "Wallet List.txt"
)


async def main(path: str) -> None:
    from app.db.database import init_db
    from app.db import repository as repo

    await init_db()
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Support both the tracker export format and our simple {x_handle,wallet,label} format.
    added = 0
    skipped = 0
    existing = {t.wallet for t in await repo.all_traders(limit=5000) if t.wallet}
    for row in data:
        wallet = (row.get("trackedWalletAddress") or row.get("wallet") or "").strip()
        name = (row.get("name") or row.get("label") or "").strip()
        handle = (row.get("x_handle") or "").strip()
        if not wallet:
            continue
        if wallet in existing:
            skipped += 1
            continue
        await repo.add_tracked_trader(handle, wallet, name or "tracked wallet", source="import")
        existing.add(wallet)
        added += 1

    print(f"Imported {added} wallets ({skipped} already present).")
    print("The agent will study them gradually via its discovery loop (Helius budget-guarded).")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT
    asyncio.run(main(path))
