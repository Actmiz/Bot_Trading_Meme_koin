"""Unit tests for Modul 4: Exit & Manajemen Posisi (Sizer, Exit Rules, PositionManager)."""

import pytest

from src.core.config import BotConfig, PositionConfig
from src.core.database import Database
from src.core.enums import BotMode, DecisionType, ExitReason, TokenSource, TradeStatus
from src.core.models import TokenSnapshot, TradeRecord
from src.execution.executor import TransactionExecutor
from src.position.exit_rules import PositionState, evaluate_exit_conditions
from src.position.manager import PositionManager
from src.position.sizer import calculate_position_size

MINT_SAMPLE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


# ---------------------------------------------------------------------------
# 1. Position Sizer Tests
# ---------------------------------------------------------------------------
def test_position_sizer_respects_pct_and_cap():
    cfg = PositionConfig(max_position_pct=2.0, max_position_sol=0.5, max_open_positions=5)

    # 2% of 10 SOL = 0.2 SOL (< 0.5 cap)
    res1 = calculate_position_size(wallet_capital_sol=10.0, config=cfg, open_positions_count=2)
    assert res1.allowed is True
    assert res1.amount_sol == pytest.approx(0.2)

    # 2% of 50 SOL = 1.0 SOL -> capped at max_position_sol (0.5 SOL)
    res2 = calculate_position_size(wallet_capital_sol=50.0, config=cfg, open_positions_count=0)
    assert res2.allowed is True
    assert res2.amount_sol == pytest.approx(0.5)


def test_position_sizer_blocks_when_max_open_positions_reached():
    cfg = PositionConfig(max_open_positions=3)
    res = calculate_position_size(wallet_capital_sol=10.0, config=cfg, open_positions_count=3)
    assert res.allowed is False
    assert res.amount_sol == 0.0
    assert "Max open positions" in (res.reason or "")


# ---------------------------------------------------------------------------
# 2. Exit Rules Unit Tests
# ---------------------------------------------------------------------------
def test_exit_rules_staged_take_profit():
    cfg = PositionConfig()
    state = PositionState(
        trade_id=1,
        token_id=1,
        mint_address=MINT_SAMPLE,
        entry_price_usd=1.0,
        entry_amount_sol=0.2,
        initial_tokens=1000.0,
        remaining_tokens=1000.0,
    )

    # +55% gain -> triggers TP1 (+50%, sell 30%)
    sig1 = evaluate_exit_conditions(state, current_price_usd=1.55, config=cfg)
    assert sig1.should_exit is True
    assert sig1.reason == ExitReason.TP1
    assert sig1.sell_pct == 30.0
    state.triggered_tp_indices.add(0)

    # +110% gain -> triggers TP2 (+100%, sell 30%)
    sig2 = evaluate_exit_conditions(state, current_price_usd=2.10, config=cfg)
    assert sig2.should_exit is True
    assert sig2.reason == ExitReason.TP2
    assert sig2.sell_pct == 30.0
    state.triggered_tp_indices.add(1)

    # +210% gain -> triggers TP3 (+200%, sell 100% remaining)
    sig3 = evaluate_exit_conditions(state, current_price_usd=3.10, config=cfg)
    assert sig3.should_exit is True
    assert sig3.reason == ExitReason.TP3
    assert sig3.sell_pct == 100.0


def test_exit_rules_hard_stop_loss():
    cfg = PositionConfig(stop_loss_pct=-30.0)
    state = PositionState(
        trade_id=1,
        token_id=1,
        mint_address=MINT_SAMPLE,
        entry_price_usd=1.0,
        entry_amount_sol=0.2,
        initial_tokens=1000.0,
        remaining_tokens=1000.0,
    )

    # -32% drop -> triggers hard stop-loss
    sig = evaluate_exit_conditions(state, current_price_usd=0.68, config=cfg)
    assert sig.should_exit is True
    assert sig.reason == ExitReason.STOP_LOSS
    assert sig.sell_pct == 100.0


def test_exit_rules_trailing_stop():
    cfg = PositionConfig()  # activation=30%, trail=20%
    state = PositionState(
        trade_id=1,
        token_id=1,
        mint_address=MINT_SAMPLE,
        entry_price_usd=1.0,
        entry_amount_sol=0.2,
        initial_tokens=1000.0,
        remaining_tokens=1000.0,
        triggered_tp_indices={0},  # Suppose TP1 already taken
    )

    # Price climbs to $1.60 (+60% gain, above +30% activation)
    sig_climb = evaluate_exit_conditions(state, current_price_usd=1.60, config=cfg)
    assert state.peak_price_usd == pytest.approx(1.60)
    assert sig_climb.should_exit is False

    # Price drops 25% from peak ($1.60 -> $1.20) -> triggers trailing stop (>= 20% trail)
    sig_drop = evaluate_exit_conditions(state, current_price_usd=1.20, config=cfg)
    assert sig_drop.should_exit is True
    assert sig_drop.reason == ExitReason.TRAILING_STOP
    assert sig_drop.sell_pct == 100.0


def test_exit_rules_time_stop():
    cfg = PositionConfig(time_stop_minutes=60)
    state = PositionState(
        trade_id=1,
        token_id=1,
        mint_address=MINT_SAMPLE,
        entry_price_usd=1.0,
        entry_amount_sol=0.2,
        initial_tokens=1000.0,
        remaining_tokens=1000.0,
        entry_timestamp=1000.0,
    )

    # 65 minutes later (3900s), price flat at $1.01 (+1%) -> triggers time-stop
    sig = evaluate_exit_conditions(
        state,
        current_price_usd=1.01,
        config=cfg,
        now_timestamp=1000.0 + 3900.0,
    )
    assert sig.should_exit is True
    assert sig.reason == ExitReason.TIME_STOP
    assert sig.sell_pct == 100.0


def test_exit_rules_emergency_liquidity_and_rug():
    cfg = PositionConfig()
    state = PositionState(
        trade_id=1,
        token_id=1,
        mint_address=MINT_SAMPLE,
        entry_price_usd=1.0,
        entry_amount_sol=0.2,
        initial_tokens=1000.0,
        remaining_tokens=1000.0,
        initial_liquidity_usd=50000.0,
    )

    # 1. Liquidity drops from $50k to $20k (-60% >= 50% threshold)
    sig_liq = evaluate_exit_conditions(
        state,
        current_price_usd=0.95,
        config=cfg,
        current_liquidity_usd=20000.0,
    )
    assert sig_liq.should_exit is True
    assert sig_liq.reason == ExitReason.EMERGENCY_LIQUIDITY

    # 2. Rug indicator detected ("lp_removed")
    sig_rug = evaluate_exit_conditions(
        state,
        current_price_usd=0.95,
        config=cfg,
        current_liquidity_usd=48000.0,
        active_rug_indicators=["lp_removed"],
    )
    assert sig_rug.should_exit is True
    assert sig_rug.reason == ExitReason.EMERGENCY_RUG


# ---------------------------------------------------------------------------
# 3. PositionManager Integration Tests (Partial TP + Full Exit + DB Sync)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_position_manager_partial_tp_then_trailing_stop():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(mode=BotMode.PAPER)
    executor = TransactionExecutor(config=config, fail_simulation_prob=0.0)
    manager = PositionManager(config=config, database=db, executor=executor)

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

    manager.register_trade(trade, initial_liquidity_usd=50000.0, entry_timestamp=1000.0)
    assert manager.open_count == 1

    # Step 1: Price rises +60% ($0.001 -> $0.0016) -> TP1 sells 30% (9,000 tokens), position stays open
    sig1 = await manager.evaluate_and_manage(
        trade_id=trade_id,
        current_price_usd=0.0016,
        current_liquidity_usd=60000.0,
        now_timestamp=1300.0,
    )
    assert sig1.reason == ExitReason.TP1
    assert manager.open_count == 1
    assert manager.open_positions[trade_id].remaining_tokens == pytest.approx(21000.0)

    # Step 2: Price drops 25% from peak ($0.0016 -> $0.0012) -> Trailing stop closes remaining 21,000 tokens
    sig2 = await manager.evaluate_and_manage(
        trade_id=trade_id,
        current_price_usd=0.0012,
        current_liquidity_usd=55000.0,
        now_timestamp=1600.0,
    )
    assert sig2.reason == ExitReason.TRAILING_STOP
    assert manager.open_count == 0

    # Verify DB trade row is closed and PnL is positive
    closed_trades = await db.get_all_closed_trades()
    assert len(closed_trades) == 1
    assert closed_trades[0]["exit_reason"] == ExitReason.TRAILING_STOP.value
    assert closed_trades[0]["pnl_sol"] > 0
    assert closed_trades[0]["holding_duration_sec"] == 600

    # Verify both partial and full exit decisions were logged
    cursor = await db.conn.execute(
        "SELECT * FROM decisions_log WHERE decision_type = ? ORDER BY id",
        (DecisionType.EXIT.value,),
    )
    exit_logs = await cursor.fetchall()
    assert len(exit_logs) == 2

    await executor.close()
    await db.close()

