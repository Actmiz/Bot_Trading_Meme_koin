"""Unit tests for src.core (config loader, models, database operations)."""

import pytest
from src.core.config import load_config, BotConfig
from src.core.enums import BotMode, TokenSource, DexType, TradeStatus, ExitReason, LPStatus
from src.core.models import TokenSnapshot, ScreeningResult, ScoringResult, TradeRecord
from src.core.database import Database


@pytest.mark.asyncio
async def test_database_lifecycle_and_crud():
    """Test connecting, inserting token, screening, scoring, trade, and closing."""
    db = Database(db_path=":memory:")
    await db.connect()

    # 1. Insert Token
    token = TokenSnapshot(
        mint_address="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        symbol="BONK",
        name="Bonk",
        source=TokenSource.DEXSCREENER,
        dex=DexType.RAYDIUM,
        initial_price_usd=0.000025,
        initial_liquidity_usd=500000.0,
        initial_volume_24h=1500000.0,
        initial_holders=12000,
    )
    token_id = await db.insert_token(token)
    assert token_id > 0

    fetched = await db.get_token_by_mint(token.mint_address)
    assert fetched is not None
    assert fetched["symbol"] == "BONK"

    # 2. Insert Screening Result
    screening = ScreeningResult(
        token_id=token_id,
        mint_address=token.mint_address,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        holder_concentration_ok=True,
        top10_holder_pct=28.5,
        liquidity_sufficient=True,
        liquidity_usd=500000.0,
        lp_status=LPStatus.BURNED,
        honeypot_safe=True,
        sell_tax_pct=0.0,
        transfer_fee_pct=0.0,
        passed=True,
    )
    screening_id = await db.insert_screening(screening)
    assert screening_id > 0

    # 3. Insert Scoring Result
    scoring = ScoringResult(
        token_id=token_id,
        screening_id=screening_id,
        feat_price_momentum=0.85,
        feat_volume_liquidity_ratio=3.0,
        feat_holder_growth_rate=0.4,
        feat_buy_sell_ratio=1.8,
        feat_token_age_score=0.9,
        total_score=0.78,
        threshold_used=0.65,
        above_threshold=True,
    )
    scoring_id = await db.insert_scoring(scoring)
    assert scoring_id > 0

    # 4. Insert Trade
    trade = TradeRecord(
        token_id=token_id,
        scoring_id=scoring_id,
        mint_address=token.mint_address,
        mode=BotMode.PAPER.value,
        entry_price_usd=0.000025,
        entry_amount_sol=0.1,
        entry_amount_tokens=4000.0,
        entry_slippage_pct=0.5,
        entry_fee_sol=0.00005,
        entry_at="2026-11-02T20:00:00Z",
        status=TradeStatus.OPEN,
    )
    trade_id = await db.insert_trade(trade)
    assert trade_id > 0

    open_trades = await db.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["id"] == trade_id

    # 5. Update Trade Exit
    await db.update_trade_exit(
        trade_id=trade_id,
        exit_price_usd=0.000030,
        exit_amount_sol=0.12,
        exit_slippage_pct=0.6,
        exit_fee_sol=0.00005,
        exit_tx_signature="sim_tx_123",
        exit_reason=ExitReason.TP1.value,
        pnl_sol=0.02,
        pnl_usd=4.0,
        pnl_pct=20.0,
        holding_duration_sec=300,
    )

    open_after = await db.get_open_positions_count() if hasattr(db, "get_open_positions_count") else await db.count_open_positions()
    assert open_after == 0

    closed_trades = await db.get_all_closed_trades()
    assert len(closed_trades) == 1
    assert closed_trades[0]["exit_reason"] == ExitReason.TP1.value

    await db.close()


def test_config_loader():
    """Test loading config.yaml and default values."""
    config = load_config()
    assert isinstance(config, BotConfig)
    assert config.mode == BotMode.PAPER
    assert config.screening.min_liquidity_usd == 10000
    assert config.scoring.threshold == 0.65
    assert sum(config.scoring.weights.values()) == pytest.approx(1.0, 0.01)
