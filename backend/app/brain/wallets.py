"""Smart-money tracker.

Maintains the reputation DB of wallets. Two jobs:
  * score early buyers of a token so the entry brain can weight "smart money is in".
  * after a trade resolves (or we judge a token a winner/loser), credit/debit the
    wallets that bought it early so the DB gets sharper over time.

This is how the agent builds its own list of profitable traders it can later lean on for
conviction, copytrading, or front-running.
"""

from __future__ import annotations

from ..db import repository as repo
from ..domain import Observation


class WalletTracker:
    async def score_early_buyers(self, wallets: list[str]) -> dict[str, float]:
        # Base score = the agent's own learned reputation for each wallet.
        scores = await repo.wallet_scores(wallets)
        # Boost wallets the agent has decided to trust/watch from its KOL discovery, so a
        # known smart-money name buying early actually registers as a signal.
        trusted = await repo.trusted_wallet_set()
        for w in wallets:
            if w in trusted:
                bump = 0.9 if trusted[w] == "trust" else 0.6
                scores[w] = max(scores.get(w, 0.0), bump)
        return scores

    async def note_creator(self, wallet: str) -> None:
        await repo.register_creator(wallet)

    async def resolve_token(self, obs: Observation, won: bool) -> None:
        """Credit/debit every early buyer we saw for this token."""
        seen: set[str] = set()
        for wl in obs.smart_buyers:
            if wl and wl not in seen:
                seen.add(wl)
                await repo.record_early_buyer(wl, won)

    async def leaderboard(self, limit: int = 30):
        return await repo.good_wallets(limit=limit, min_score=0.4)
