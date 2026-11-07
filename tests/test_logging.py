"""Unit tests for Modul 6: Logging, Secret Redaction, and Telegram Notifications."""

import logging
import pytest

from src.core.config import BotConfig, EnvSecrets, TelegramConfig
from src.core.database import Database
from src.core.enums import BotMode, DecisionType, TokenSource, TradeStatus
from src.core.models import DecisionLog, ScreeningResult, ScoringResult, TokenSnapshot, TradeRecord
from src.logging_.telegram import TelegramNotifier
from src.logging_.trade_logger import SecretRedactingFilter, TradeAuditLogger

MINT_SAMPLE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def test_secret_redacting_filter_scrubs_private_keys_and_tokens():
    fake_privkey = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ89aBcDeFgHiJkLmNoPqRsTuVwXyZ"
    fake_api_key = "helius_secret_key_998877"
    filt = SecretRedactingFilter(secrets_to_redact=[fake_privkey, fake_api_key])

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=f"Connecting with key {fake_api_key} and wallet {fake_privkey}",
        args=(),
        exc_info=None,
    )
    filt.filter(record)

    assert fake_api_key not in record.msg
    assert fake_privkey not in record.msg
    assert "[REDACTED_SECRET]" in record.msg


@pytest.mark.asyncio
async def test_trade_audit_logger_records_decisions_and_skip_summary():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(
        mode=BotMode.PAPER,
        telegram=TelegramConfig(enabled=False, notify_on=["entry", "exit", "error", "risk_limit"]),
    )
    audit = TradeAuditLogger(config=config, database=db)

    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="BONK",
        source=TokenSource.DEXSCREENER,
        initial_liquidity_usd=50000.0,
    )
    token_id = await db.insert_token(token)

    # 1. Simulate 2 screening skips and 1 score skip
    await db.insert_decision(
        DecisionLog(
            decision_type=DecisionType.SKIP_SCREENING,
            mint_address=MINT_SAMPLE,
            token_id=token_id,
            reason="Mint authority active",
            details_json={"fail_reasons": ["Mint authority active: held by X", "LP is not burned or locked"]},
        )
    )
    await db.insert_decision(
        DecisionLog(
            decision_type=DecisionType.SKIP_SCREENING,
            mint_address=MINT_SAMPLE,
            token_id=token_id,
            reason="Mint authority active",
            details_json={"fail_reasons": ["Mint authority active: held by Y"]},
        )
    )
    await db.insert_decision(
        DecisionLog(
            decision_type=DecisionType.SKIP_SCORE,
            mint_address=MINT_SAMPLE,
            token_id=token_id,
            reason="Score 0.45 < threshold 0.65",
        )
    )

    summary = await audit.get_skip_summary()
    assert summary["skip_screening_count"] == 2
    assert summary["skip_score_count"] == 1
    assert summary["total_skipped"] == 3
    assert summary["gate_failure_counts"]["Mint authority active"] == 2
    assert summary["gate_failure_counts"]["LP is not burned or locked"] == 1

    # 2. Log entry & exit and verify Telegram queue
    screening = ScreeningResult(
        token_id=token_id,
        mint_address=MINT_SAMPLE,
        top10_holder_pct=22.0,
        liquidity_usd=50000.0,
        passed=True,
    )
    screening.id = await db.insert_screening(screening)

    scoring = ScoringResult(
        token_id=token_id,
        screening_id=screening.id,
        feat_price_momentum=0.8,
        feat_volume_liquidity_ratio=0.7,
        feat_holder_growth_rate=0.6,
        feat_buy_sell_ratio=0.75,
        feat_token_age_score=0.9,
        total_score=0.76,
        threshold_used=0.65,
        above_threshold=True,
    )
    scoring.id = await db.insert_scoring(scoring)

    trade = TradeRecord(
        token_id=token_id,
        scoring_id=scoring.id,
        mint_address=MINT_SAMPLE,
        mode=BotMode.PAPER.value,
        entry_price_usd=0.000025,
        entry_amount_sol=0.2,
        entry_amount_tokens=1200000.0,
        entry_slippage_pct=0.45,
        entry_fee_sol=0.00005,
        entry_score=0.76,
        status=TradeStatus.OPEN,
    )
    trade.id = await db.insert_trade(trade)

    await audit.log_entry(trade=trade, screening=screening, scoring=scoring, symbol="BONK")
    await audit.log_exit(
        trade_id=trade.id,
        token_id=token_id,
        mint_address=MINT_SAMPLE,
        reason="tp1",
        exit_price_usd=0.000038,
        slippage_pct=0.5,
        fee_sol=0.00005,
        pnl_sol=0.09,
        pnl_pct=45.0,
        holding_sec=420,
        symbol="BONK",
    )
    await audit.log_error(context="RPC Poll", error_msg="Timeout", mint_address=MINT_SAMPLE)
    await audit.log_risk_alert(event_type="circuit_breaker", reason="3 failures", daily_pnl_sol=-0.3, drawdown_pct=3.0)

    categories = [cat for cat, _ in audit.notifier.sent_messages]
    assert categories == ["entry", "exit", "error", "risk_limit"]

    await audit.notifier.close()
    await db.close()


@pytest.mark.asyncio
async def test_telegram_command_polling_authorization():
    config = BotConfig(
        telegram=TelegramConfig(enabled=False, notify_on=["risk_limit", "daily_report"]),
        secrets=EnvSecrets(telegram_bot_token="dummy_tok", telegram_chat_id="12345"),
    )
    notifier = TelegramNotifier(config=config)

    kill_reasons: list[str] = []

    async def on_kill(reason: str) -> None:
        kill_reasons.append(reason)

    async def on_status() -> str:
        return "Bot status: OK"

    mock_updates = [
        # Unauthorized chat_id -> must be ignored!
        {"update_id": 1, "message": {"chat": {"id": "99999"}, "text": "/kill hacker attempt"}},
        # Authorized chat_id -> must be processed!
        {"update_id": 2, "message": {"chat": {"id": "12345"}, "text": "/status"}},
        {"update_id": 3, "message": {"chat": {"id": "12345"}, "text": "/kill emergency drain"}},
    ]

    processed = await notifier.poll_commands(
        on_kill_command=on_kill,
        on_status_command=on_status,
        mock_updates=mock_updates,
    )

    assert processed == ["/status", "/kill"]
    assert kill_reasons == ["emergency drain"]

    await notifier.close()

