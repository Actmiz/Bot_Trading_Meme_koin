"""Message formatters for Telegram notifications and structured audit logs."""

from __future__ import annotations

from typing import Any

from src.core.models import TradeRecord


def format_entry_message(
    trade: TradeRecord,
    symbol: str | None = None,
    score: float | None = None,
    features: dict[str, Any] | None = None,
) -> str:
    """Format a trade entry notification message."""
    sym = symbol or trade.mint_address[:8]
    sc = score if score is not None else (trade.entry_score or 0.0)
    feat_str = ""
    if features:
        parts = [f"{k}={v:.2f}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in features.items()]
        feat_str = f"\nFeatures: {', '.join(parts)}"

    return (
        f"🟢 [ENTRY - {trade.mode}] {sym} ({trade.mint_address[:8]}...)\n"
        f"Amount: {trade.entry_amount_sol or 0.0:.4f} SOL -> {trade.entry_amount_tokens or 0.0:,.2f} tokens\n"
        f"Fill Price: ${trade.entry_price_usd or 0.0:.8f} | Slippage: {trade.entry_slippage_pct or 0.0:.2f}%\n"
        f"Fee: {trade.entry_fee_sol or 0.0:.6f} SOL | Score: {sc:.4f}"
        f"{feat_str}"
    )


def format_exit_message(
    mint_address: str,
    reason: str,
    pnl_sol: float,
    pnl_pct: float,
    exit_price_usd: float,
    slippage_pct: float = 0.0,
    fee_sol: float = 0.0,
    holding_sec: int = 0,
    symbol: str | None = None,
    partial: bool = False,
) -> str:
    """Format a trade exit (partial or full) notification message."""
    sym = symbol or mint_address[:8]
    tag = "PARTIAL EXIT" if partial else "EXIT"
    icon = "💰" if pnl_sol >= 0 else "🔻"
    return (
        f"{icon} [{tag}] {sym} ({mint_address[:8]}...)\n"
        f"Reason: {reason}\n"
        f"Exit Price: ${exit_price_usd:.8f} | Slippage: {slippage_pct:.2f}% | Fee: {fee_sol:.6f} SOL\n"
        f"PnL: {pnl_sol:+.4f} SOL ({pnl_pct:+.2f}%) | Held: {holding_sec}s"
    )


def format_error_message(
    context: str,
    error_msg: str,
    mint_address: str | None = None,
) -> str:
    """Format an error alert notification message."""
    mint_part = f" | Mint: {mint_address[:8]}..." if mint_address else ""
    return f"⚠️ [ERROR] {context}{mint_part}\nDetail: {error_msg}"


def format_risk_event_message(
    event_type: str,
    reason: str,
    daily_pnl_sol: float = 0.0,
    drawdown_pct: float = 0.0,
) -> str:
    """Format a risk limit breach / circuit breaker / kill switch notification message."""
    return (
        f"🚨 [RISK ALERT - {event_type.upper()}]\n"
        f"Reason: {reason}\n"
        f"Daily PnL: {daily_pnl_sol:+.4f} SOL | Drawdown: {drawdown_pct:.2f}%"
    )


def format_daily_report_message(
    date_str: str,
    total_trades: int,
    win_rate: float,
    net_pnl_sol: float,
    skipped_screening: int = 0,
    skipped_score: int = 0,
) -> str:
    """Format a daily summary report message."""
    return (
        f"📊 [DAILY REPORT - {date_str}]\n"
        f"Trades Closed: {total_trades} | Win Rate: {win_rate:.1f}%\n"
        f"Net PnL: {net_pnl_sol:+.4f} SOL\n"
        f"Skipped (Screening): {skipped_screening} | Skipped (Score): {skipped_score}"
    )

