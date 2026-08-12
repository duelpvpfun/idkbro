"""Pump.fun launch safety — lightweight, learnable gates.

We deliberately DON'T check mint/freeze authority: pump.fun revokes them by design. The
only hard filters here are the two the user cares about up front:

  * dev holding > 8%  -> skip (dev can dump on us)
  * sniped/bundled >= 15% -> skip (too much early concentration = exit liquidity trap)

Everything else (which narratives rug, what buy patterns fail) the agent learns on its
own by paper trading the real market. These thresholds live in config so the agent can
propose refinements over time.
"""

from __future__ import annotations

from ..config import settings
from ..domain import Observation, SafetyReport


def evaluate(obs: Observation) -> SafetyReport:
    reasons: list[str] = []
    passed = True

    if obs.dev_holding_pct > settings.max_dev_holding_pct:
        reasons.append(
            f"dev holds {obs.dev_holding_pct:.1f}% (> {settings.max_dev_holding_pct:.0f}%)"
        )
        passed = False

    if obs.sniped_pct >= settings.max_sniped_pct:
        reasons.append(
            f"{obs.sniped_pct:.1f}% sniped/bundled early "
            f"(>= {settings.max_sniped_pct:.0f}%, {obs.bundled_wallets} wallets)"
        )
        passed = False

    # Holder concentration (bubblemap-style). -1 means unknown (no Helius key) -> ignore.
    # Only hard-block on extreme clustering; new pump.fun tokens are naturally a bit
    # concentrated, so we keep this threshold high and let the brain judge the grey zone.
    if 0 <= settings.max_top10_holding_pct < obs.top10_holding_pct:
        reasons.append(
            f"top-10 hold {obs.top10_holding_pct:.0f}% "
            f"(> {settings.max_top10_holding_pct:.0f}%, rug/cluster risk)"
        )
        passed = False

    if passed and not reasons:
        reasons.append("dev + sniper + holder concentration within limits")
    return SafetyReport(passed=passed, reasons=reasons)
