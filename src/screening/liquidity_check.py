"""Screening Gate 3: Liquidity and LP Status (Burned / Locked).

Verifies:
1. Liquidity USD >= min_liquidity_usd.
2. LP tokens are either 100% burned (sent to dead address / incinerator)
   or locked in a verified locker (e.g. Streamflow, Raydium lock).
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.enums import LPStatus

logger = logging.getLogger(__name__)

# Known burn / dead addresses on Solana
BURN_ADDRESSES = {
    "11111111111111111111111111111111",
    "dead111111111111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
}


def check_liquidity_and_lp(
    liquidity_usd: float | None,
    min_liquidity_usd: float = 10000.0,
    lp_must_be_burned_or_locked: bool = True,
    lp_burned_pct: float | None = None,
    lp_locked_pct: float | None = None,
    rugcheck_lp_status: str | None = None,
) -> tuple[bool, LPStatus, list[str]]:
    """
    Check if liquidity is sufficient and LP is burned/locked.

    Parameters
    ----------
    liquidity_usd               : Current pool liquidity in USD.
    min_liquidity_usd           : Minimum acceptable pool liquidity.
    lp_must_be_burned_or_locked : Flag requiring LP to be burned or locked.
    lp_burned_pct               : Percentage of LP burned (0-100).
    lp_locked_pct               : Percentage of LP locked (0-100).
    rugcheck_lp_status          : Status reported by RugCheck ('burned', 'locked', 'unlocked').

    Returns
    -------
    (passed, lp_status, fail_reasons)
    """
    fail_reasons: list[str] = []

    # 1. Check liquidity amount
    if liquidity_usd is None or liquidity_usd < min_liquidity_usd:
        actual_str = f"${liquidity_usd:,.0f}" if liquidity_usd is not None else "Unknown"
        fail_reasons.append(
            f"Liquidity insufficient: {actual_str} < required ${min_liquidity_usd:,.0f}"
        )

    # 2. Determine LP Status
    lp_status = LPStatus.UNKNOWN

    if rugcheck_lp_status:
        status_clean = rugcheck_lp_status.lower()
        if "burn" in status_clean or "100" in status_clean:
            lp_status = LPStatus.BURNED
        elif "lock" in status_clean:
            lp_status = LPStatus.LOCKED
        else:
            lp_status = LPStatus.UNLOCKED

    if lp_status == LPStatus.UNKNOWN:
        total_safe_pct = (lp_burned_pct or 0.0) + (lp_locked_pct or 0.0)
        if total_safe_pct >= 95.0:
            lp_status = LPStatus.BURNED if (lp_burned_pct or 0.0) >= 95.0 else LPStatus.LOCKED
        elif total_safe_pct > 0.0:
            lp_status = LPStatus.UNLOCKED
        else:
            lp_status = LPStatus.UNLOCKED

    # 3. Check requirement
    if lp_must_be_burned_or_locked:
        if lp_status not in (LPStatus.BURNED, LPStatus.LOCKED):
            fail_reasons.append(
                f"LP is not burned or locked (status={lp_status.value})"
            )

    passed = len(fail_reasons) == 0
    return passed, lp_status, fail_reasons
