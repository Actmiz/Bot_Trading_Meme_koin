"""PositionManager — tracks open trades, executes partial/full exits, and updates DB."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import DecisionType, ExitReason
from src.core.models import DecisionLog, TradeRecord
from src.execution.executor import TransactionExecutor
from src.position.exit_rules import PositionState, ExitSignal, evaluate_exit_conditions
from src.position.sizer import PositionSizeResult, calculate_position_size

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Manages all currently open positions:
    - Enforces max concurrent open positions.
    - Evaluates TP1/TP2/TP3, Stop-Loss, Trailing Stop, Time-Stop, and Emergency Exits.
    - Executes partial and full sells via TransactionExecutor.
    - Records exit decisions and updates final trade PnL in SQLite.
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        executor: TransactionExecutor | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.executor = executor or TransactionExecutor(config)
        self._positions: dict[int, PositionState] = {}

    @property
    def open_positions(self) -> dict[int, PositionState]:
        """Return currently tracked open positions keyed by trade_id."""
        return self._positions

    @property
    def open_count(self) -> int:
        """Number of currently open positions."""
        return len(self._positions)

    def can_open_position(self, wallet_capital_sol: float) -> PositionSizeResult:
        """Check if a new position can be opened and calculate its SOL size."""
        return calculate_position_size(
            wallet_capital_sol=wallet_capital_sol,
            config=self.config.position,
            open_positions_count=self.open_count,
        )

    def register_trade(
        self,
        trade: TradeRecord,
        initial_liquidity_usd: float | None = None,
        entry_timestamp: float | None = None,
    ) -> PositionState:
        """Register a newly opened TradeRecord for active position monitoring."""
        if trade.id is None:
            raise ValueError("Cannot register trade without a valid database id")

        ts = entry_timestamp
        if ts is None and trade.entry_at:
            try:
                ts = datetime.fromisoformat(trade.entry_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = datetime.now(timezone.utc).timestamp()
        elif ts is None:
            ts = datetime.now(timezone.utc).timestamp()

        state = PositionState(
            trade_id=trade.id,
            token_id=trade.token_id,
            mint_address=trade.mint_address,
            entry_price_usd=trade.entry_price_usd or 0.0,
            entry_amount_sol=trade.entry_amount_sol or 0.0,
            initial_tokens=trade.entry_amount_tokens or 0.0,
            remaining_tokens=trade.entry_amount_tokens or 0.0,
            entry_fee_sol=trade.entry_fee_sol or 0.0,
            initial_liquidity_usd=initial_liquidity_usd,
            entry_timestamp=ts,
        )
        self._positions[trade.id] = state
        logger.info(
            "Registered open position [trade_id=%d, mint=%s, tokens=%.2f, entry=$%.6f]",
            trade.id, trade.mint_address, state.remaining_tokens, state.entry_price_usd
        )
        return state

    async def evaluate_and_manage(
        self,
        trade_id: int,
        current_price_usd: float,
        current_liquidity_usd: float | None = None,
        active_rug_indicators: list[str] | None = None,
        now_timestamp: float | None = None,
    ) -> ExitSignal:
        """
        Evaluate exit rules for a single open position and execute partial/full exit if triggered.
        """
        state = self._positions.get(trade_id)
        if state is None:
            return ExitSignal(should_exit=False)

        signal = evaluate_exit_conditions(
            state=state,
            current_price_usd=current_price_usd,
            config=self.config.position,
            current_liquidity_usd=current_liquidity_usd,
            active_rug_indicators=active_rug_indicators,
            now_timestamp=now_timestamp,
        )

        if not signal.should_exit or signal.reason is None:
            return signal

        await self._execute_exit(
            state=state,
            signal=signal,
            current_price_usd=current_price_usd,
            current_liquidity_usd=current_liquidity_usd,
            now_timestamp=now_timestamp,
        )
        return signal

    async def _execute_exit(
        self,
        state: PositionState,
        signal: ExitSignal,
        current_price_usd: float,
        current_liquidity_usd: float | None = None,
        now_timestamp: float | None = None,
    ) -> bool:
        """Execute a partial or full sell order for the position."""
        is_full_exit = signal.sell_pct >= 99.9
        tokens_to_sell = (
            state.remaining_tokens
            if is_full_exit
            else state.remaining_tokens * (signal.sell_pct / 100.0)
        )

        exec_res = await self.executor.execute_sell(
            mint_address=state.mint_address,
            token_amount=tokens_to_sell,
            current_price_usd=current_price_usd,
            liquidity_usd=current_liquidity_usd or state.initial_liquidity_usd,
        )

        if not exec_res.success:
            logger.error(
                "Exit sell failed for trade #%d (%s): %s",
                state.trade_id, state.mint_address, exec_res.error_message
            )
            await self.db.insert_decision(
                DecisionLog(
                    decision_type=DecisionType.ERROR,
                    mint_address=state.mint_address,
                    token_id=state.token_id,
                    trade_id=state.trade_id,
                    reason=f"Exit order failed ({signal.reason.value}): {exec_res.error_message}",
                )
            )
            return False

        # Update state accounting
        state.realized_sol += exec_res.amount_sol
        state.realized_fee_sol += exec_res.fee_sol
        state.remaining_tokens = max(0.0, state.remaining_tokens - tokens_to_sell)

        if signal.tp_index is not None:
            state.triggered_tp_indices.add(signal.tp_index)

        # Log exit decision (partial or full)
        await self.db.insert_decision(
            DecisionLog(
                decision_type=DecisionType.EXIT,
                mint_address=state.mint_address,
                token_id=state.token_id,
                trade_id=state.trade_id,
                reason=f"{signal.reason.value}: {signal.description}",
                details_json={
                    "partial": not is_full_exit,
                    "sell_pct": signal.sell_pct,
                    "tokens_sold": tokens_to_sell,
                    "remaining_tokens": state.remaining_tokens,
                    "fill_price_usd": exec_res.fill_price_usd,
                    "received_sol": exec_res.amount_sol,
                    "tx_signature": exec_res.tx_signature,
                },
            )
        )

        # If position is completely closed, finalize in SQLite trades table
        if is_full_exit or state.remaining_tokens <= 1e-9:
            now_ts = (
                now_timestamp
                if now_timestamp is not None
                else datetime.now(timezone.utc).timestamp()
            )
            holding_sec = max(0, int(now_ts - state.entry_timestamp))
            total_fees_sol = state.entry_fee_sol + state.realized_fee_sol
            pnl_sol = state.realized_sol - state.entry_amount_sol - total_fees_sol
            sol_usd = self.config.execution.paper_sol_price_usd
            pnl_usd = pnl_sol * sol_usd
            pnl_pct = (
                (pnl_sol / state.entry_amount_sol) * 100.0
                if state.entry_amount_sol > 0
                else 0.0
            )

            await self.db.update_trade_exit(
                trade_id=state.trade_id,
                exit_price_usd=exec_res.fill_price_usd,
                exit_amount_sol=round(state.realized_sol, 6),
                exit_slippage_pct=exec_res.actual_slippage_pct,
                exit_fee_sol=round(state.realized_fee_sol, 6),
                exit_tx_signature=exec_res.tx_signature,
                exit_reason=signal.reason.value,
                pnl_sol=round(pnl_sol, 6),
                pnl_usd=round(pnl_usd, 4),
                pnl_pct=round(pnl_pct, 2),
                holding_duration_sec=holding_sec,
            )

            self._positions.pop(state.trade_id, None)
            logger.info(
                "POSITION CLOSED [Trade #%d] %s | Reason=%s | PnL=%+.4f SOL (%+.2f%%)",
                state.trade_id, state.mint_address, signal.reason.value, pnl_sol, pnl_pct
            )
        else:
            logger.info(
                "PARTIAL EXIT [Trade #%d] %s | Reason=%s | Sold=%.1f%% | Remaining=%.2f tokens",
                state.trade_id, state.mint_address, signal.reason.value,
                signal.sell_pct, state.remaining_tokens
            )

        return True

    async def close_all_positions(
        self,
        reason: ExitReason,
        price_lookup: dict[str, float] | None = None,
    ) -> int:
        """
        Force-close all currently open positions (used by Kill Switch / Risk Controller).
        Returns the number of positions closed.
        """
        closed_count = 0
        price_lookup = price_lookup or {}

        for trade_id in list(self._positions.keys()):
            state = self._positions.get(trade_id)
            if state is None:
                continue
            price = price_lookup.get(state.mint_address, state.entry_price_usd)
            signal = ExitSignal(
                should_exit=True,
                sell_pct=100.0,
                reason=reason,
                description=f"Forced close all positions ({reason.value})",
            )
            ok = await self._execute_exit(
                state=state,
                signal=signal,
                current_price_usd=price,
            )
            if ok:
                closed_count += 1

        return closed_count

