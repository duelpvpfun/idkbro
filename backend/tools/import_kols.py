"""Import named KOL X handles (e.g. cupsey, ga__ke, a1lon9) for the agent to read + study.

These are handle-first (may have no wallet). The agent will read a few of their posts
(quota-capped) and factor their vibe into its own identity + meta understanding. It still
decides trust/watch/ignore on its own.

Run:  source .venv/bin/activate && python -m tools.import_kols
"""

from __future__ import annotations

import asyncio
import json
import os

_SEED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "seed_kols.json")


async def main() -> None:
    from app.db.database import init_db
    from app.db import repository as repo

    await init_db()
    if not os.path.exists(_SEED):
        print(f"No seed file at {_SEED}")
        return
    with open(_SEED, "r", encoding="utf-8") as f:
        rows = json.load(f)

    existing = {t.x_handle.lower() for t in await repo.all_traders(limit=5000) if t.x_handle}
    added = 0
    for r in rows:
        handle = str(r.get("x_handle", "")).lstrip("@").strip()
        if not handle or handle.lower() in existing:
            continue
        await repo.add_tracked_trader(handle, "", str(r.get("label", "")), source="kol")
        existing.add(handle.lower())
        added += 1
        print(f"queued @{handle} for study")

    print(f"\nAdded {added} KOL handles. The agent will read + judge them via its discovery loop.")


if __name__ == "__main__":
    asyncio.run(main())
