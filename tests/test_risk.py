"""Unit tests for Modul 5: Risk Control (Hard Limits, Kill Switch, Circuit Breaker, Daily Loss, Max Drawdown)."""

import pytest

from src.core.config import BotConfig, CircuitBreakerConfig, RiskConfig
from src.core.database import Database
from src.core.enums import BotMode, ExitReason, RiskEventType, TokenSource, TradeStatus
from src.core.models import TokenSnapshot, TradeRecord
from src.execution.executor import TransactionExecutor
from src.position.manager import PositionManager
from src.risk.controller import RiskController
from src.risk.limits import (
    HARD_RISK_LIMITS,
    enforce_hard_risk_ceilings,
    is_protected_parameter,
)

MINT_SAMPLE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


# ---------------------------------------------------------------------------
# 1. Hard-Coded Ceilings & Protected Parameter Guard
# ---------------------------------------------------------------------------
def test_hard_risk_ceilings_clamp_excessive_config():
    # Attempt to configure dangerously loose risk limits
    loose_cfg = RiskConfig(
        daily_loss_limit_sol=999.0,
        max_drawdown_pct=95.0,
        circuit_breaker=CircuitBreakerConfig(consecutive_failures=100, pause_duration_sec=5),
    )
    effective = enforce_hard_risk_ceilings(loose_cfg)

    assert effective.daily_loss_limit_sol == HARD_RISK_LIMITS.max_daily_loss_sol
    assert effective.max_drawdown_pct == HARD_RISK_LIMITS.max_drawdown_pct
    assert effective.consecutive_failures == HARD_RISK_LIMITS.max_consecutive_failures
    assert effective.pause_duration_sec == HARD_RISK_LIMITS.min_pause_duration_sec


def test_is_protected_parameter_blocks_risk_and_screening():
    assert is_protected_parameter("risk.daily_loss_limit_sol") is True
    assert is_protected_parameter("risk.max_drawdown_pct") is True
    assert is_protected_parameter("screening.top10_holder_max_pct") is True
    assert is_protected_parameter("screening.honeypot_safe") is True
    assert is_protected_parameter("bot.live_confirm_required") is True

    # Scoring & position parameters are allowed to be proposed by optimizer
    assert is_protected_parameter("scoring.threshold") is False
    assert is_protected_parameter("scoring.weights.price_momentum") is False
    assert is_protected_parameter("position.stop_loss_pct") is False


# ---------------------------------------------------------------------------
# 2. Dedicated Small Bot Wallet Validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wallet_balance_validation(tmp_path):
    db = Database(db_path=":memory:")
    await db.connect()

    rc = RiskController(
        config=BotConfig(),
        database=db,
        initial_capital_sol=5.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )

    # Too small (< 0.02 SOL)
    ok_low, reason_low = rc.validate_wallet_balance(0.005)
    assert ok_low is False
    assert "below minimum" in (reason_low or "")

    # Safe small dedicated bot wallet (e.g. 5 SOL)
    ok_mid, reason_mid = rc.validate_wallet_balance(5.0)
    assert ok_mid is True
    assert reason_mid is None

    # Too large (> 20 SOL hot wallet ceiling)
    ok_high, reason_high = rc.validate_wallet_balance(50.0)
    assert ok_high is False
    assert "cold storage" in (reason_high or "")

    await db.close()


# ---------------------------------------------------------------------------
# 3. Daily Loss Limit Halts Bot & Closes Positions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_loss_limit_halts_and_records_event(tmp_path):
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(
        mode=BotMode.PAPER,
        risk=RiskConfig(daily_loss_limit_sol=1.0, max_drawdown_pct=20.0),
    )
    rc = RiskController(
        config=config,
        database=db,
        initial_capital_sol=10.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )

    # First loss: -0.6 SOL (still within 1.0 SOL daily limit)
    res1 = await rc.record_trade_closed(pnl_sol=-0.6, date_str="2026-11-05", now_timestamp=1000.0)
    assert res1.allowed is True
    assert rc.is_halted is False

    # Second loss: -0.5 SOL -> total daily PnL = -1.1 SOL (breaches -1.0 SOL limit)
    res2 = await rc.record_trade_closed(pnl_sol=-0.5, date_str="2026-11-05", now_timestamp=1100.0)
    assert res2.allowed is False
    assert res2.event_type == RiskEventType.DAILY_LOSS_LIMIT
    assert rc.is_halted is True

    # Verify risk_events row was recorded in SQLite
    cursor = await db.conn.execute(
        "SELECT * FROM risk_events WHERE event_type = ?",
        (RiskEventType.DAILY_LOSS_LIMIT.value,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["daily_pnl_sol"] == pytest.approx(-1.1)

    await db.close()


# ---------------------------------------------------------------------------
# 4. Max Drawdown From Peak Capital Halts Bot
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_max_drawdown_from_peak_halts_bot(tmp_path):
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(
        mode=BotMode.PAPER,
        risk=RiskConfig(daily_loss_limit_sol=5.0, max_drawdown_pct=15.0),
    )
    rc = RiskController(
        config=config,
        database=db,
        initial_capital_sol=10.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )

    # Win +10 SOL -> Peak capital becomes 20.0 SOL
    await rc.record_trade_closed(pnl_sol=10.0, date_str="2026-11-05", now_timestamp=1000.0)
    assert rc.peak_capital_sol == pytest.approx(20.0)

    # Lose -3.2 SOL on next day -> Current capital = 16.8 SOL -> Drawdown = 3.2 / 20.0 = 16.0% (>= 15%)
    res = await rc.record_trade_closed(pnl_sol=-3.2, date_str="2026-11-06", now_timestamp=90000.0)
    assert res.allowed is False
    assert res.event_type == RiskEventType.MAX_DRAWDOWN
    assert rc.is_halted is True
    assert rc.current_drawdown_pct == pytest.approx(16.0)

    await db.close()


# ---------------------------------------------------------------------------
# 5. Circuit Breaker Pauses After Consecutive Failures & Resumes Later
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_pause_and_resume(tmp_path):
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(
        risk=RiskConfig(
            circuit_breaker=CircuitBreakerConfig(consecutive_failures=3, pause_duration_sec=300)
        )
    )
    rc = RiskController(
        config=config,
        database=db,
        initial_capital_sol=10.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )

    # 2 failures -> not tripped yet
    assert await rc.record_transaction_result(False, "timeout 1", now_timestamp=1000.0) is False
    assert await rc.record_transaction_result(False, "timeout 2", now_timestamp=1010.0) is False

    # 3rd consecutive failure -> trips circuit breaker (paused until 1020 + 300 = 1320)
    tripped = await rc.record_transaction_result(False, "timeout 3", now_timestamp=1020.0)
    assert tripped is True

    # Check during pause window (t=1100) -> blocked
    chk_during = await rc.check_can_trade(now_timestamp=1100.0)
    assert chk_during.allowed is False
    assert chk_during.event_type == RiskEventType.CIRCUIT_BREAKER

    # Check after pause window expires (t=1350) -> allowed again!
    chk_after = await rc.check_can_trade(now_timestamp=1350.0)
    assert chk_after.allowed is True

    # A successful tx resets consecutive_failures to 0
    await rc.record_transaction_result(True, now_timestamp=1360.0)
    assert rc.consecutive_failures == 0

    await db.close()


# ---------------------------------------------------------------------------
# 6. Kill Switch (File Flag & Telegram) Force-Closes All Open Positions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kill_switch_file_and_telegram_close_all_positions(tmp_path):
    db = Database(db_path=":memory:")
    await db.connect()

    ks_file = tmp_path / "KILL_SWITCH"
    config = BotConfig(mode=BotMode.PAPER)
    executor = TransactionExecutor(config=config, fail_simulation_prob=0.0)
    pos_manager = PositionManager(config=config, database=db, executor=executor)

    rc = RiskController(
        config=config,
        database=db,
        position_manager=pos_manager,
        initial_capital_sol=10.0,
        kill_switch_path=ks_file,
    )

    # Insert and register an open position
    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="BONK",
        source=TokenSource.DEXSCREENER,
        initial_liquidity_usd=50000.0,
    )
    token_id = await db.insert_token(token)
    trade = TradeRecord(
        token_id=token_id,
        mint_address=MINT_SAMPLE,
        mode=BotMode.PAPER.value,
        entry_price_usd=0.001,
        entry_amount_sol=0.2,
        entry_amount_tokens=30000.0,
        entry_fee_sol=0.00005,
        entry_at="2026-11-05T10:00:00Z",
        status=TradeStatus.OPEN,
    )
    trade_id = await db.insert_trade(trade)
    trade.id = trade_id
    pos_manager.register_trade(trade, initial_liquidity_usd=50000.0)
    assert pos_manager.open_count == 1

    # Trigger Telegram kill switch
    closed = await rc.trigger_telegram_kill(
        reason="Emergency stop by user",
        price_lookup={MINT_SAMPLE: 0.0011},
    )
    assert closed == 1
    assert pos_manager.open_count == 0
    assert rc.is_halted is True
    assert ks_file.exists()

    # Verify trade in DB was closed with reason 'kill_switch'
    closed_trades = await db.get_all_closed_trades()
    assert len(closed_trades) == 1
    assert closed_trades[0]["exit_reason"] == ExitReason.KILL_SWITCH.value

    await executor.close()
    await db.close()

