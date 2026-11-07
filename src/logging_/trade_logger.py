"""TradeAuditLogger & SecretRedactingFilter — comprehensive decision logging and secret protection."""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import DecisionType
from src.core.models import DecisionLog, ScreeningResult, ScoringResult, TradeRecord
from src.logging_.formatters import (
    format_daily_report_message,
    format_entry_message,
    format_error_message,
    format_exit_message,
    format_risk_event_message,
)
from src.logging_.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

# Matches base58 strings of length 64-90 (typical Solana keypair private keys)
_BASE58_PRIVKEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,90}\b")


class SecretRedactingFilter(logging.Filter):
    """
    Logging filter that scrubs any secret values (private keys, API keys, bot tokens)
    from log messages and arguments before they reach any file or console handler.
    """

    def __init__(self, secrets_to_redact: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets_to_redact or []) if s and len(s) >= 6]

    def redact_text(self, text: str) -> str:
        redacted = text
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "[REDACTED_SECRET]")
        redacted = _BASE58_PRIVKEY_RE.sub("[REDACTED_PRIVATE_KEY]", redacted)
        return redacted

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self.redact_text(a) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self.redact_text(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True


def setup_logging(
    config: BotConfig,
    log_file: str | Path = "./logs/bot.log",
) -> SecretRedactingFilter:
    """
    Configure root logger with RotatingFileHandler and SecretRedactingFilter
    so that private keys or API secrets can NEVER be written to logs.
    """
    secrets_list = [
        config.secrets.wallet_private_key,
        config.secrets.telegram_bot_token,
        config.secrets.helius_api_key,
        config.secrets.birdeye_api_key,
    ]
    redact_filter = SecretRedactingFilter(secrets_to_redact=secrets_list)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    root_logger.addFilter(redact_filter)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Attach filter to all existing handlers
    for handler in root_logger.handlers:
        handler.addFilter(redact_filter)

    # Add console StreamHandler if none exists so output is visible in terminal
    has_console_handler = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root_logger.handlers
    )
    if not has_console_handler:
        ch = logging.StreamHandler()
        ch.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        ch.addFilter(redact_filter)
        root_logger.addHandler(ch)

    # Add rotating file handler if not already added for this path
    has_file_handler = any(
        isinstance(h, RotatingFileHandler) and Path(getattr(h, "baseFilename", "")) == log_path.resolve()
        for h in root_logger.handlers
    )
    if not has_file_handler:
        fh = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        fh.addFilter(redact_filter)
        root_logger.addHandler(fh)

    return redact_filter


class TradeAuditLogger:
    """
    Unified audit logger & notifier for Modul 6:
    - Records every decision (entry, exit, skip_screening, skip_score, error, risk_triggered)
      with full context into SQLite `decisions_log`.
    - Dispatches formatted Telegram notifications.
    - Provides skip-statistics queries to evaluate whether screening/scoring is too strict.
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.notifier = notifier or TelegramNotifier(config)

    async def log_entry(
        self,
        trade: TradeRecord,
        screening: ScreeningResult | None = None,
        scoring: ScoringResult | None = None,
        symbol: str | None = None,
    ) -> int:
        """Record trade entry with full feature/screening/score context and notify Telegram."""
        features_dict: dict[str, Any] = {}
        if scoring is not None:
            features_dict = {
                "price_momentum": scoring.feat_price_momentum,
                "volume_liquidity_ratio": scoring.feat_volume_liquidity_ratio,
                "holder_growth": scoring.feat_holder_growth_rate,
                "buy_sell_ratio": scoring.feat_buy_sell_ratio,
                "token_age": scoring.feat_token_age_score,
            }
        elif trade.entry_features_json and "features" in trade.entry_features_json:
            features_dict = trade.entry_features_json["features"]

        details = {
            "entry_price_usd": trade.entry_price_usd,
            "entry_amount_sol": trade.entry_amount_sol,
            "entry_amount_tokens": trade.entry_amount_tokens,
            "entry_slippage_pct": trade.entry_slippage_pct,
            "entry_fee_sol": trade.entry_fee_sol,
            "entry_score": trade.entry_score,
            "features": features_dict,
            "screening_passed": screening.passed if screening else True,
            "top10_holder_pct": screening.top10_holder_pct if screening else None,
            "liquidity_usd": screening.liquidity_usd if screening else None,
        }

        decision_id = await self.db.insert_decision(
            DecisionLog(
                decision_type=DecisionType.ENTRY,
                mint_address=trade.mint_address,
                token_id=trade.token_id,
                trade_id=trade.id,
                reason=f"Entry executed at ${trade.entry_price_usd or 0.0:.8f} (score={trade.entry_score or 0.0:.4f})",
                details_json=details,
            )
        )

        msg = format_entry_message(
            trade=trade,
            symbol=symbol,
            score=trade.entry_score,
            features=features_dict,
        )
        await self.notifier.send_message("entry", msg)
        return decision_id

    async def log_exit(
        self,
        trade_id: int,
        token_id: int,
        mint_address: str,
        reason: str,
        exit_price_usd: float,
        slippage_pct: float,
        fee_sol: float,
        pnl_sol: float,
        pnl_pct: float,
        holding_sec: int,
        symbol: str | None = None,
        partial: bool = False,
    ) -> int:
        """Record trade exit with actual slippage, fee, PnL, and exit reason, and notify Telegram."""
        decision_id = await self.db.insert_decision(
            DecisionLog(
                decision_type=DecisionType.EXIT,
                mint_address=mint_address,
                token_id=token_id,
                trade_id=trade_id,
                reason=reason,
                details_json={
                    "partial": partial,
                    "exit_price_usd": exit_price_usd,
                    "actual_slippage_pct": slippage_pct,
                    "fee_sol": fee_sol,
                    "pnl_sol": pnl_sol,
                    "pnl_pct": pnl_pct,
                    "holding_duration_sec": holding_sec,
                },
            )
        )

        msg = format_exit_message(
            mint_address=mint_address,
            reason=reason,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
            exit_price_usd=exit_price_usd,
            slippage_pct=slippage_pct,
            fee_sol=fee_sol,
            holding_sec=holding_sec,
            symbol=symbol,
            partial=partial,
        )
        await self.notifier.send_message("exit", msg)
        return decision_id

    async def log_error(
        self,
        context: str,
        error_msg: str,
        mint_address: str | None = None,
        token_id: int | None = None,
        trade_id: int | None = None,
    ) -> int:
        """Record an operational/execution error and notify Telegram."""
        decision_id = await self.db.insert_decision(
            DecisionLog(
                decision_type=DecisionType.ERROR,
                mint_address=mint_address,
                token_id=token_id,
                trade_id=trade_id,
                reason=f"{context}: {error_msg}",
                details_json={"context": context, "error": error_msg},
            )
        )
        msg = format_error_message(context=context, error_msg=error_msg, mint_address=mint_address)
        await self.notifier.send_message("error", msg)
        return decision_id

    async def log_risk_alert(
        self,
        event_type: str,
        reason: str,
        daily_pnl_sol: float = 0.0,
        drawdown_pct: float = 0.0,
    ) -> None:
        """Send a Telegram risk limit notification."""
        msg = format_risk_event_message(
            event_type=event_type,
            reason=reason,
            daily_pnl_sol=daily_pnl_sol,
            drawdown_pct=drawdown_pct,
        )
        await self.notifier.send_message("risk_limit", msg)

    async def get_skip_summary(self) -> dict[str, Any]:
        """
        Aggregate skipped tokens from `decisions_log` so the operator (and Modul 7)
        can evaluate whether screening gates or scoring thresholds are overly strict.
        """
        cursor = await self.db.conn.execute(
            "SELECT decision_type, reason, details_json FROM decisions_log "
            "WHERE decision_type IN (?, ?)",
            (DecisionType.SKIP_SCREENING.value, DecisionType.SKIP_SCORE.value),
        )
        rows = await cursor.fetchall()

        skip_screening_count = 0
        skip_score_count = 0
        gate_failure_counts: dict[str, int] = {}

        for row in rows:
            dtype = row["decision_type"]
            if dtype == DecisionType.SKIP_SCREENING.value:
                skip_screening_count += 1
                raw_details = row["details_json"]
                reasons: list[str] = []
                if raw_details:
                    try:
                        parsed = json.loads(raw_details)
                        reasons = parsed.get("fail_reasons") or []
                    except json.JSONDecodeError:
                        reasons = [row["reason"] or "unknown"]
                for r in reasons:
                    key = r.split(":")[0].strip()
                    gate_failure_counts[key] = gate_failure_counts.get(key, 0) + 1
            elif dtype == DecisionType.SKIP_SCORE.value:
                skip_score_count += 1

        return {
            "skip_screening_count": skip_screening_count,
            "skip_score_count": skip_score_count,
            "total_skipped": skip_screening_count + skip_score_count,
            "gate_failure_counts": gate_failure_counts,
        }

