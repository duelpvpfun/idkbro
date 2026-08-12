"""Global Helius call budget.

Every Helius request in the app checks in here first. It enforces a hard daily cap so no
loop (especially wallet discovery over hundreds of addresses) can ever run away and eat
your monthly credit. Counts reset each day. Purely in-process, which is fine for one agent.
"""

from __future__ import annotations

import time

from ..config import settings


class HeliusBudget:
    def __init__(self) -> None:
        self._day = int(time.time() // 86_400)
        self._used = 0

    def _roll(self) -> None:
        today = int(time.time() // 86_400)
        if today != self._day:
            self._day = today
            self._used = 0

    def can_spend(self, n: int = 1) -> bool:
        self._roll()
        return self._used + n <= settings.helius_max_calls_per_day

    def spend(self, n: int = 1) -> None:
        self._roll()
        self._used += n

    @property
    def used_today(self) -> int:
        self._roll()
        return self._used

    @property
    def remaining_today(self) -> int:
        self._roll()
        return max(0, settings.helius_max_calls_per_day - self._used)


helius_budget = HeliusBudget()
