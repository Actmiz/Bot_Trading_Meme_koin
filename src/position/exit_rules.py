"""Exit rules evaluation — Staged Take-Profit, Stop-Loss, Trailing Stop, Time-Stop, and Emergency Exits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.core.config import PositionConfig
from src.core.enums import ExitReason


@dataclass
class PositionState:
    """In-memory state tracking an open position."""

    trade_id: int
    token_id: int
    mint_address: str
    entry_price_usd: float
    entry_amount_sol: float
    initial_tokens: float
    remaining_tokens: float
    entry_fee_sol: float = 0.0
    initial_liquidity_usd: float | None = None
    entry_timestamp: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())

    # Dynamic tracking
    peak_price_usd: float = 0.0
    triggered_tp_indices: set[int] = field(default_factory=set)
    realized_sol: float = 0.0
    realized_fee_sol: float = 0.0
    stop_at_entry: bool = False

    def __post_init__(self) -> None:
        if self.peak_price_usd <= 0.0:
            self.peak_price_usd = self.entry_price_usd


@dataclass(frozen=True)
class ExitSignal:
    """Decision returned by exit rule evaluation."""

    should_exit: bool
    sell_pct: float = 0.0  # Percentage of remaining_tokens to sell (0 - 100)
    reason: ExitReason | None = None
    description: str = ""
    tp_index: int | None = None


_TP_ENUM_MAP = {
    0: ExitReason.TP1,
    1: ExitReason.TP2,
    2: ExitReason.TP3,
}


def evaluate_exit_conditions(
    state: PositionState,
    current_price_usd: float,
    config: PositionConfig,
    current_liquidity_usd: float | None = None,
    active_rug_indicators: list[str] | None = None,
    now_timestamp: float | None = None,
    time_stop_max_gain_pct: float = 5.0,
) -> ExitSignal:
    """
    Evaluate all exit rules for an open position in priority order:
    1. Emergency Rug Indicator
    2. Emergency Liquidity Drop
    3. Hard Stop-Loss
    4. Trailing Stop
    5. Staged Take-Profit (TP1 / TP2 / TP3)
    6. Time-Stop (stagnant position after N minutes)
    """
    if state.entry_price_usd <= 0 or state.remaining_tokens <= 0:
        return ExitSignal(should_exit=False)

    # Update peak price
    if current_price_usd > state.peak_price_usd:
        state.peak_price_usd = current_price_usd

    # ------------------------------------------------------------------
    # 1. Emergency Exit: Rug Indicators
    # ------------------------------------------------------------------
    if active_rug_indicators:
        configured_triggers = set(config.emergency.rug_indicators)
        matched = [ind for ind in active_rug_indicators if ind in configured_triggers]
        if matched:
            return ExitSignal(
                should_exit=True,
                sell_pct=100.0,
                reason=ExitReason.EMERGENCY_RUG,
                description=f"Emergency rug indicator detected: {', '.join(matched)}",
            )

    # ------------------------------------------------------------------
    # 2. Emergency Exit: Drastic Liquidity Drop
    # ------------------------------------------------------------------
    if (
        state.initial_liquidity_usd
        and state.initial_liquidity_usd > 0
        and current_liquidity_usd is not None
    ):
        liq_drop_pct = (
            (state.initial_liquidity_usd - current_liquidity_usd)
            / state.initial_liquidity_usd
        ) * 100.0
        if liq_drop_pct >= config.emergency.liquidity_drop_pct:
            return ExitSignal(
                should_exit=True,
                sell_pct=100.0,
                reason=ExitReason.EMERGENCY_LIQUIDITY,
                description=(
                    f"Liquidity dropped {liq_drop_pct:.1f}% "
                    f"(${state.initial_liquidity_usd:,.0f} -> ${current_liquidity_usd:,.0f})"
                ),
            )

    # Current PnL % relative to entry price
    pnl_pct = ((current_price_usd - state.entry_price_usd) / state.entry_price_usd) * 100.0

    # ------------------------------------------------------------------
    # 3. Hard Stop-Loss (or Breakeven Stop at Entry)
    # ------------------------------------------------------------------
    effective_stop_pct = 0.0 if state.stop_at_entry else config.stop_loss_pct
    if pnl_pct <= effective_stop_pct:
        return ExitSignal(
            should_exit=True,
            sell_pct=100.0,
            reason=ExitReason.STOP_LOSS,
            description=f"Stop-loss triggered at {pnl_pct:.2f}% (limit {effective_stop_pct:.1f}%)",
        )

    # ------------------------------------------------------------------
    # 4. Trailing Stop
    # ------------------------------------------------------------------
    peak_gain_pct = ((state.peak_price_usd - state.entry_price_usd) / state.entry_price_usd) * 100.0
    if peak_gain_pct >= config.trailing_stop.activation_pct and state.peak_price_usd > 0:
        drop_from_peak_pct = (
            (state.peak_price_usd - current_price_usd) / state.peak_price_usd
        ) * 100.0
        if drop_from_peak_pct >= config.trailing_stop.trail_pct:
            return ExitSignal(
                should_exit=True,
                sell_pct=100.0,
                reason=ExitReason.TRAILING_STOP,
                description=(
                    f"Trailing stop triggered: dropped {drop_from_peak_pct:.1f}% from peak "
                    f"${state.peak_price_usd:.6f} (peak gain +{peak_gain_pct:.1f}%)"
                ),
            )

    # ------------------------------------------------------------------
    # 5. Staged Take-Profit (TP1, TP2, TP3)
    # ------------------------------------------------------------------
    for idx, tp_level in enumerate(config.take_profit_levels):
        if idx not in state.triggered_tp_indices and pnl_pct >= tp_level.pct:
            reason = _TP_ENUM_MAP.get(idx, ExitReason.TP3)
            return ExitSignal(
                should_exit=True,
                sell_pct=tp_level.sell_pct,
                reason=reason,
                description=(
                    f"{reason.value.upper()} reached at +{pnl_pct:.1f}% "
                    f"(target +{tp_level.pct:.0f}%, selling {tp_level.sell_pct:.0f}%)"
                ),
                tp_index=idx,
            )

    # ------------------------------------------------------------------
    # 6. Time-Stop (keluar jika tidak bergerak dalam N menit)
    # ------------------------------------------------------------------
    now_ts = now_timestamp if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
    elapsed_minutes = (now_ts - state.entry_timestamp) / 60.0
    if elapsed_minutes >= config.time_stop_minutes and abs(pnl_pct) <= time_stop_max_gain_pct:
        return ExitSignal(
            should_exit=True,
            sell_pct=100.0,
            reason=ExitReason.TIME_STOP,
            description=(
                f"Time-stop triggered: held {elapsed_minutes:.1f}m "
                f"(>= {config.time_stop_minutes}m) with flat PnL ({pnl_pct:+.2f}%)"
            ),
        )

    return ExitSignal(should_exit=False)

