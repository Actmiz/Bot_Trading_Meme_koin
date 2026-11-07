"""Unit tests for Modul 3: Scoring & Entry Engine."""

import pytest

from src.core.config import BotConfig, ScoringConfig
from src.core.database import Database
from src.core.enums import TokenSource, DexType, DecisionType, TradeStatus
from src.core.models import TokenSnapshot, ScreeningResult
from src.scoring.features import extract_scoring_features, ScoringFeatures
from src.scoring.scorer import TokenScorer
from src.scoring.entry import EntryEngine
from src.execution.executor import TransactionExecutor


MINT_SAMPLE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def test_feature_extraction_bounds():
    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="BONK",
        source=TokenSource.DEXSCREENER,
        initial_price_usd=0.000025,
        initial_liquidity_usd=50000.0,
        initial_volume_24h=150000.0,
        initial_holders=850,
        initial_buy_count=1200,
        initial_sell_count=600,
        token_age_seconds=600,
    )

    features = extract_scoring_features(token, price_change_5m_pct=25.0)

    # All features must strictly lie in [0.0, 1.0]
    for feat_name, val in features.to_dict().items():
        assert 0.0 <= val <= 1.0, f"Feature {feat_name} out of bounds: {val}"

    # Bullish values should produce high normalized scores
    assert features.price_momentum > 0.7
    assert features.volume_liquidity_ratio > 0.6
    assert features.buy_sell_ratio > 0.6


@pytest.mark.asyncio
async def test_scorer_above_and_below_threshold():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(scoring=ScoringConfig(threshold=0.65))
    scorer = TokenScorer(config=config, database=db)

    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="BONK",
        source=TokenSource.DEXSCREENER,
        initial_liquidity_usd=50000.0,
    )
    token_id = await db.insert_token(token)
    screening_id = await db.insert_screening(
        ScreeningResult(token_id=token_id, mint_address=MINT_SAMPLE, passed=True)
    )

    # 1. High scoring features -> above threshold
    high_features = ScoringFeatures(
        price_momentum=0.9,
        volume_liquidity_ratio=0.8,
        holder_growth=0.8,
        buy_sell_ratio=0.85,
        token_age=0.9,
    )
    res_high = await scorer.score_token(
        token=token,
        token_id=token_id,
        screening_id=screening_id,
        override_features=high_features,
    )
    assert res_high.total_score >= 0.65
    assert res_high.above_threshold is True

    # 2. Low scoring features -> below threshold (should log skip)
    low_features = ScoringFeatures(
        price_momentum=0.3,
        volume_liquidity_ratio=0.2,
        holder_growth=0.3,
        buy_sell_ratio=0.3,
        token_age=0.3,
    )
    res_low = await scorer.score_token(
        token=token,
        token_id=token_id,
        screening_id=screening_id,
        override_features=low_features,
    )
    assert res_low.total_score < 0.65
    assert res_low.above_threshold is False

    # Check skip recorded in decisions_log
    cursor = await db.conn.execute(
        "SELECT * FROM decisions_log WHERE decision_type = ?",
        (DecisionType.SKIP_SCORE.value,)
    )
    skip_log = await cursor.fetchone()
    assert skip_log is not None
    assert skip_log["token_id"] == token_id

    await db.close()


@pytest.mark.asyncio
async def test_entry_engine_full_flow():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig()
    executor = TransactionExecutor(config=config)
    entry_engine = EntryEngine(config=config, database=db, executor=executor)

    token = TokenSnapshot(
        mint_address=MINT_SAMPLE,
        symbol="BONK",
        source=TokenSource.DEXSCREENER,
        initial_price_usd=0.000025,
        initial_liquidity_usd=50000.0,
        initial_volume_24h=200000.0,
        initial_holders=1500,
        initial_buy_count=2000,
        initial_sell_count=800,
        token_age_seconds=400,
    )
    token_id = await db.insert_token(token)

    # 1. If screening failed, entry is skipped
    failed_screening = ScreeningResult(
        token_id=token_id,
        mint_address=token.mint_address,
        passed=False,
    )
    trade_none = await entry_engine.evaluate_and_enter(
        token=token,
        token_id=token_id,
        screening_result=failed_screening,
    )
    assert trade_none is None

    # 2. If screening passed and score is high -> executes buy order & creates trade
    passed_screening = ScreeningResult(
        token_id=token_id,
        mint_address=token.mint_address,
        passed=True,
    )
    passed_screening.id = await db.insert_screening(passed_screening)
    trade = await entry_engine.evaluate_and_enter(
        token=token,
        token_id=token_id,
        screening_result=passed_screening,
        wallet_capital_sol=5.0,
        price_change_5m_pct=30.0,
    )

    assert trade is not None
    assert trade.id is not None
    assert trade.status == TradeStatus.OPEN
    assert trade.entry_amount_sol > 0
    assert trade.entry_amount_tokens > 0
    assert trade.entry_tx_signature is not None
    assert trade.mode == "PAPER"

    # Verify open trade exists in DB
    open_trades = await db.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["mint_address"] == token.mint_address

    # Verify entry decision was logged
    cursor = await db.conn.execute(
        "SELECT * FROM decisions_log WHERE decision_type = ?",
        (DecisionType.ENTRY.value,)
    )
    entry_log = await cursor.fetchone()
    assert entry_log is not None
    assert entry_log["trade_id"] == trade.id

    # Features (not weights) must be snapshotted for post-trade analysis
    assert "features" in trade.entry_features_json
    assert trade.scoring_id is not None

    await executor.close()
    await db.close()

