"""RiskController — enforces daily loss limits, max drawdown, circuit breaker, kill switch, and wallet balance checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import DecisionType, ExitReason, RiskEventType
from src.core.models import DecisionLog, RiskEvent
from src.position.manager import PositionManager
from src.risk.kill_switch import KillSwitch
from src.risk.limits import (
    HARD_RISK_LIMITS,
    EffectiveRiskLimits,
    enforce_hard_risk_ceilings,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskCheckResult:
    """Result of a pre-trade or periodic risk check."""

    allowed: bool
    event_type: RiskEventType | None = None
    reason: str | None = None


class RiskController:
    """
    Central risk controller that cannot be bypassed or overridden by the learning module:
    - Enforces daily loss limit (SOL) -> halts bot and closes open positions.
    - Enforces maximum drawdown (%) from peak equity -> halts bot and closes open positions.
    - Enforces circuit breaker after N consecutive transaction failures -> pauses bot for M seconds.
    - Enforces kill switch (file flag or Telegram command) -> halts bot and closes all positions.
    - Validates dedicated small bot wallet balance.
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        position_manager: PositionManager | None = None,
        initial_capital_sol: float = 10.0,
        kill_switch_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.position_manager = position_manager

        # Clamp config limits against hard-coded safety ceilings
        self.limits: EffectiveRiskLimits = enforce_hard_risk_ceilings(config.risk)

        ks_path = kill_switch_path if kill_switch_path is not None else self.limits.kill_switch_file
        self.kill_switch = KillSwitch(file_path=ks_path)

        # Equity & drawdown tracking
        self.initial_capital_sol = max(0.01, initial_capital_sol)
        self.current_capital_sol = self.initial_capital_sol
        self.peak_capital_sol = self.initial_capital_sol

        # Daily PnL tracking (keyed by YYYY-MM-DD)
        self._daily_pnl_by_date: dict[str, float] = {}

        # Circuit breaker state
        self.consecutive_failures: int = 0
        self.paused_until_ts: float = 0.0

        # Halt state
        self.is_halted: bool = False
        self.halt_reason: str | None = None

    @property
    def current_drawdown_pct(self) -> float:
        """Calculate current percentage drawdown from peak capital."""
        if self.peak_capital_sol <= 0:
            return 0.0
        dd = ((self.peak_capital_sol - self.current_capital_sol) / self.peak_capital_sol) * 100.0
        return round(max(0.0, dd), 4)

    def get_daily_pnl_sol(self, date_str: str | None = None) -> float:
        """Return realized PnL (SOL) for the given UTC date (YYYY-MM-DD)."""
        day = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return round(self._daily_pnl_by_date.get(day, 0.0), 6)

    def validate_wallet_balance(self, balance_sol: float) -> tuple[bool, str | None]:
        """
        Verify that the dedicated bot wallet has sufficient minimum balance and does not
        exceed the safe hot-wallet ceiling.
        """
        if balance_sol < HARD_RISK_LIMITS.min_bot_wallet_sol:
            return (
                False,
                f"Wallet balance {balance_sol:.4f} SOL is below minimum {HARD_RISK_LIMITS.min_bot_wallet_sol:.4f} SOL",
            )
        if balance_sol > HARD_RISK_LIMITS.max_bot_wallet_sol:
            return (
                False,
                (
                    f"Wallet balance {balance_sol:.2f} SOL exceeds safe dedicated bot wallet ceiling "
                    f"({HARD_RISK_LIMITS.max_bot_wallet_sol:.2f} SOL). Move excess funds to cold storage."
                ),
            )
        return True, None

    async def check_can_trade(
        self,
        now_timestamp: float | None = None,
        date_str: str | None = None,
    ) -> RiskCheckResult:
        """
        Run all risk gates before allowing a new trade entry.
        If kill switch, daily loss limit, or max drawdown is breached, halts the bot
        and closes open positions.
        """
        now_ts = now_timestamp if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
        day = date_str or datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        # 1. Kill switch check (file flag or Telegram command)
        ks_active, ks_reason = self.kill_switch.is_active()
        if ks_active:
            if not self.is_halted:
                await self._trigger_halt(
                    event_type=RiskEventType.KILL_SWITCH,
                    exit_reason=ExitReason.KILL_SWITCH,
                    reason=ks_reason or "Kill switch active",
                    action_taken="close_all_and_halt",
                    day=day,
                )
            return RiskCheckResult(
                allowed=False,
                event_type=RiskEventType.KILL_SWITCH,
                reason=self.halt_reason or ks_reason,
            )

        # 2. Already halted check
        if self.is_halted:
            return RiskCheckResult(
                allowed=False,
                event_type=None,
                reason=f"Bot is halted: {self.halt_reason}",
            )

        # 3. Circuit breaker pause check
        if now_ts < self.paused_until_ts:
            remaining = int(self.paused_until_ts - now_ts)
            return RiskCheckResult(
                allowed=False,
                event_type=RiskEventType.CIRCUIT_BREAKER,
                reason=f"Circuit breaker active ({remaining}s remaining after {self.consecutive_failures} failures)",
            )

        # 4. Daily loss limit check (sync with DB + in-memory)
        db_daily_pnl = await self.db.get_daily_pnl(day)
        mem_daily_pnl = self._daily_pnl_by_date.get(day, 0.0)
        # Use whichever reflects greater loss or tracked state
        effective_daily_pnl = min(db_daily_pnl, mem_daily_pnl) if db_daily_pnl != 0 else mem_daily_pnl

        if effective_daily_pnl <= -self.limits.daily_loss_limit_sol:
            reason = (
                f"Daily loss limit reached: {effective_daily_pnl:.4f} SOL "
                f"(limit -{self.limits.daily_loss_limit_sol:.4f} SOL)"
            )
            await self._trigger_halt(
                event_type=RiskEventType.DAILY_LOSS_LIMIT,
                exit_reason=ExitReason.DAILY_LIMIT,
                reason=reason,
                action_taken="close_all_and_halt",
                day=day,
                daily_pnl_override=effective_daily_pnl,
            )
            return RiskCheckResult(
                allowed=False,
                event_type=RiskEventType.DAILY_LOSS_LIMIT,
                reason=reason,
            )

        # 5. Max drawdown check
        dd_pct = self.current_drawdown_pct
        if dd_pct >= self.limits.max_drawdown_pct:
            reason = (
                f"Max drawdown reached: {dd_pct:.2f}% "
                f"(limit {self.limits.max_drawdown_pct:.2f}%, peak={self.peak_capital_sol:.4f} SOL, "
                f"current={self.current_capital_sol:.4f} SOL)"
            )
            await self._trigger_halt(
                event_type=RiskEventType.MAX_DRAWDOWN,
                exit_reason=ExitReason.MAX_DRAWDOWN,
                reason=reason,
                action_taken="close_all_and_halt",
                day=day,
            )
            return RiskCheckResult(
                allowed=False,
                event_type=RiskEventType.MAX_DRAWDOWN,
                reason=reason,
            )

        # 6. Max open positions check
        open_count = (
            self.position_manager.open_count
            if self.position_manager is not None
            else await self.db.count_open_positions()
        )
        if open_count >= self.config.position.max_open_positions:
            reason = f"Max open positions reached ({open_count}/{self.config.position.max_open_positions})"
            await self._log_risk_event(
                event_type=RiskEventType.MAX_POSITIONS,
                reason=reason,
                action_taken="reject_entry",
                day=day,
            )
            return RiskCheckResult(
                allowed=False,
                event_type=RiskEventType.MAX_POSITIONS,
                reason=reason,
            )

        return RiskCheckResult(allowed=True)

    async def record_trade_closed(
        self,
        pnl_sol: float,
        date_str: str | None = None,
        now_timestamp: float | None = None,
    ) -> RiskCheckResult:
        """
        Update capital, peak equity, and daily PnL when a trade closes, and immediately
        check if daily loss limit or max drawdown has been breached.
        """
        now_ts = now_timestamp if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
        day = date_str or datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        self.current_capital_sol = round(self.current_capital_sol + pnl_sol, 6)
        if self.current_capital_sol > self.peak_capital_sol:
            self.peak_capital_sol = self.current_capital_sol

        self._daily_pnl_by_date[day] = round(self._daily_pnl_by_date.get(day, 0.0) + pnl_sol, 6)

        return await self.check_can_trade(now_timestamp=now_ts, date_str=day)

    async def record_transaction_result(
        self,
        success: bool,
        error_message: str | None = None,
        now_timestamp: float | None = None,
    ) -> bool:
        """
        Track transaction success/failure for the circuit breaker.
        Returns True if the circuit breaker was tripped on this call.
        """
        now_ts = now_timestamp if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
        day = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        if success:
            if self.consecutive_failures > 0:
                logger.debug("Transaction succeeded — resetting consecutive_failures from %d to 0", self.consecutive_failures)
            self.consecutive_failures = 0
            return False

        self.consecutive_failures += 1
        logger.warning(
            "Transaction failure #%d/%d: %s",
            self.consecutive_failures,
            self.limits.consecutive_failures,
            error_message or "unknown error",
        )

        if self.consecutive_failures >= self.limits.consecutive_failures:
            self.paused_until_ts = now_ts + self.limits.pause_duration_sec
            reason = (
                f"Circuit breaker tripped after {self.consecutive_failures} consecutive tx failures. "
                f"Pausing for {self.limits.pause_duration_sec}s."
            )
            logger.error(reason)
            await self._log_risk_event(
                event_type=RiskEventType.CIRCUIT_BREAKER,
                reason=reason,
                action_taken="pause_bot",
                day=day,
            )
            return True

        return False

    async def trigger_telegram_kill(
        self,
        reason: str = "Telegram /kill command received",
        price_lookup: dict[str, float] | None = None,
    ) -> int:
        """
        Activate kill switch from Telegram command, halt bot, and close all open positions.
        Returns number of positions closed.
        """
        full_reason = self.kill_switch.activate(source="telegram", reason=reason, create_file=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return await self._trigger_halt(
            event_type=RiskEventType.KILL_SWITCH,
            exit_reason=ExitReason.KILL_SWITCH,
            reason=full_reason,
            action_taken="close_all_and_halt",
            day=day,
            price_lookup=price_lookup,
        )

    async def _trigger_halt(
        self,
        event_type: RiskEventType,
        exit_reason: ExitReason,
        reason: str,
        action_taken: str,
        day: str,
        daily_pnl_override: float | None = None,
        price_lookup: dict[str, float] | None = None,
    ) -> int:
        """Halt the bot, close all open positions if PositionManager is attached, and log to DB."""
        self.is_halted = True
        self.halt_reason = reason
        logger.critical("BOT HALTED [%s]: %s", event_type.value, reason)

        closed_count = 0
        if self.position_manager is not None and self.position_manager.open_count > 0:
            closed_count = await self.position_manager.close_all_positions(
                reason=exit_reason,
                price_lookup=price_lookup,
            )

        await self._log_risk_event(
            event_type=event_type,
            reason=reason,
            action_taken=action_taken,
            day=day,
            daily_pnl_override=daily_pnl_override,
            extra_details={"closed_positions": closed_count},
        )
        return closed_count

    async def _log_risk_event(
        self,
        event_type: RiskEventType,
        reason: str,
        action_taken: str,
        day: str,
        daily_pnl_override: float | None = None,
        extra_details: dict[str, Any] | None = None,
    ) -> None:
        """Persist RiskEvent and DecisionLog to SQLite."""
        daily_pnl = (
            daily_pnl_override
            if daily_pnl_override is not None
            else self._daily_pnl_by_date.get(day, 0.0)
        )
        details: dict[str, Any] = {"reason": reason}
        if extra_details:
            details.update(extra_details)

        event = RiskEvent(
            event_type=event_type,
            details=details,
            action_taken=action_taken,
            daily_pnl_sol=daily_pnl,
            total_drawdown_pct=self.current_drawdown_pct,
            consecutive_failures=self.consecutive_failures,
        )
        await self.db.insert_risk_event(event)

        await self.db.insert_decision(
            DecisionLog(
                decision_type=DecisionType.RISK_TRIGGERED,
                reason=f"[{event_type.value}] {reason}",
                details_json={
                    "action_taken": action_taken,
                    "daily_pnl_sol": daily_pnl,
                    "drawdown_pct": self.current_drawdown_pct,
                    "consecutive_failures": self.consecutive_failures,
                },
            )
        )

