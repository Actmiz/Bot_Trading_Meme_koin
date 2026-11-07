"""Position sizer — calculates safe position size in SOL based on capital and open position limits."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.config import PositionConfig


@dataclass(frozen=True)
class PositionSizeResult:
    """Result of position sizing calculation."""

    allowed: bool
    amount_sol: float
    reason: str | None = None


def calculate_position_size(
    wallet_capital_sol: float,
    config: PositionConfig,
    open_positions_count: int = 0,
    min_position_sol: float = 0.001,
) -> PositionSizeResult:
    """
    Calculate position size in SOL while enforcing:
    1. Maximum concurrent open positions (`max_open_positions`).
    2. Percentage of bot capital (`max_position_pct`).
    3. Absolute SOL cap (`max_position_sol`).
    4. Available wallet capital check.
    """
    if open_positions_count >= config.max_open_positions:
        return PositionSizeResult(
            allowed=False,
            amount_sol=0.0,
            reason=f"Max open positions reached ({open_positions_count}/{config.max_open_positions})",
        )

    if wallet_capital_sol < min_position_sol:
        return PositionSizeResult(
            allowed=False,
            amount_sol=0.0,
            reason=f"Insufficient capital ({wallet_capital_sol:.4f} SOL < min {min_position_sol:.4f} SOL)",
        )

    pct_sol = wallet_capital_sol * (config.max_position_pct / 100.0)
    sized_sol = min(pct_sol, config.max_position_sol, wallet_capital_sol)
    sized_sol = round(max(min_position_sol, min(sized_sol, wallet_capital_sol)), 6)

    return PositionSizeResult(
        allowed=True,
        amount_sol=sized_sol,
        reason=None,
    )

