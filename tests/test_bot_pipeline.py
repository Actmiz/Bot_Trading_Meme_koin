"""End-to-end integration tests for MemeTradingBot across Modules 1–7."""

import pytest

from src.bot import LIVE_CONFIRMATION_PHRASE, MemeTradingBot, verify_live_mode_confirmation
from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import BotMode, DexType, TokenSource
from src.core.exceptions import LiveModeError
from src.core.models import TokenSnapshot

MINT_GOOD = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
MINT_BAD = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


@pytest.mark.asyncio
async def test_full_bot_pipeline_end_to_end():
    db = Database(db_path=":memory:")
    config = BotConfig(mode=BotMode.PAPER)
    bot = MemeTradingBot(config=config, database=db, initial_capital_sol=10.0)
    await bot.initialize()

    # 1. Candidate with unrevoked mint authority -> rejected by Modul 2 (Gate Keras)
    bad_token = TokenSnapshot(
        mint_address=MINT_BAD,
        symbol="SCAM",
        source=TokenSource.DEXSCREENER,
        dex=DexType.RAYDIUM,
        initial_price_usd=0.0001,
        initial_liquidity_usd=25000.0,
        initial_volume_24h=80000.0,
        initial_holders=400,
        initial_buy_count=500,
        initial_sell_count=200,
        token_age_seconds=300,
    )
    bad_trade = await bot.process_candidate_token(
        bad_token,
        mock_screening_kwargs={
            "mock_authorities": {"mintAuthority": "Rugger11111111111111111111111111111111", "freezeAuthority": None},
            "mock_holders": [{"address": "H1", "uiAmount": 100.0}],
            "mock_total_supply": 1000.0,
            "mock_lp_burned_pct": 100.0,
            "mock_simulation": {"can_sell": True},
            "mock_rugcheck": {"score": 80, "risks": []},
        },
    )
    assert bad_trade is None
    assert bot.position_manager.open_count == 0

    # 2. Clean candidate with strong momentum -> passes Screening & Scoring -> opens PAPER position
    good_token = TokenSnapshot(
        mint_address=MINT_GOOD,
        symbol="BONK",
        source=TokenSource.DEXSCREENER,
        dex=DexType.RAYDIUM,
        initial_price_usd=0.001,
        initial_liquidity_usd=60000.0,
        initial_volume_24h=250000.0,
        initial_holders=1800,
        initial_buy_count=2400,
        initial_sell_count=900,
        token_age_seconds=450,
    )
    trade = await bot.process_candidate_token(
        good_token,
        price_change_5m_pct=35.0,
        mock_screening_kwargs={
            "mock_authorities": {"mintAuthority": None, "freezeAuthority": None},
            "mock_holders": [{"address": f"H{i}", "uiAmount": 15000.0} for i in range(10)],
            "mock_total_supply": 1000000.0,
            "mock_lp_burned_pct": 100.0,
            "mock_simulation": {"can_sell": True, "sell_tax_pct": 0.0, "transfer_fee_pct": 0.0},
            "mock_rugcheck": {"score": 90, "risks": []},
            "mock_deployer": {"total_deploys": 1, "rugged_deploys": 0},
        },
    )
    assert trade is not None
    assert bot.position_manager.open_count == 1

    # 3. Monitor position: price rises +70% -> TP1 partial sell, then drops -> trailing stop full exit
    entry_px = trade.entry_price_usd or 0.001
    await bot.monitor_open_positions(
        price_by_mint={MINT_GOOD: entry_px * 1.70},
        liquidity_by_mint={MINT_GOOD: 75000.0},
    )
    assert bot.position_manager.open_count == 1  # Still open after TP1 (sold 30%)

    closed_ids = await bot.monitor_open_positions(
        price_by_mint={MINT_GOOD: entry_px * 1.30},  # >20% drop from peak (1.70 -> 1.30)
        liquidity_by_mint={MINT_GOOD: 70000.0},
    )
    assert closed_ids == [trade.id]
    assert bot.position_manager.open_count == 0
    assert bot.risk_controller.current_capital_sol > 10.0  # Profitable trade increased bot capital

    await bot.shutdown()


def test_live_confirmation_phrase_gate():
    cfg_locked = BotConfig(mode=BotMode.LIVE, live_confirm_required=True)
    with pytest.raises(LiveModeError):
        verify_live_mode_confirmation(cfg_locked, interactive_input=LIVE_CONFIRMATION_PHRASE)

    cfg_unlocked = BotConfig(mode=BotMode.LIVE, live_confirm_required=False)
    with pytest.raises(LiveModeError):
        verify_live_mode_confirmation(cfg_unlocked, interactive_input="wrong phrase")

    # Exact confirmation phrase succeeds
    verify_live_mode_confirmation(cfg_unlocked, interactive_input=LIVE_CONFIRMATION_PHRASE)

