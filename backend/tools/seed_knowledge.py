"""Teach the agent why big Solana coins ran (the metas), as remembered CONTEXT.

Loads data/seed_knowledge.json and feeds each entry through the same knowledge pipeline
the dashboard uses. The agent paraphrases each into its own pattern takeaway. This is NOT
rules it obeys, it's understanding it can pattern-match against future coins.

Run:  source .venv/bin/activate && python -m tools.seed_knowledge
"""

from __future__ import annotations

import asyncio
import json
import os

_SEED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "seed_knowledge.json")


async def main() -> None:
    from app.db.database import init_db
    from app.db import repository as repo
    from app.brain.advice import knowledge_processor

    await init_db()
    if not os.path.exists(_SEED):
        print(f"No seed file at {_SEED}")
        return
    with open(_SEED, "r", encoding="utf-8") as f:
        rows = json.load(f)

    for r in rows:
        topic = str(r.get("topic", "")).strip()
        text = str(r.get("text", "")).strip()
        if not text:
            continue
        note = await repo.add_market_knowledge(text, topic=topic, category="meta")
        await knowledge_processor.process(note.id, text, topic)
        print(f"taught: {topic}")

    print(f"\nDone. Fed {len(rows)} meta lessons. The agent remembers them as context.")


if __name__ == "__main__":
    asyncio.run(main())
