"""Unit tests for Modul 7: Belajar dari Kesalahan (Analyzer, Walk-Forward Backtester, Optimizer, Versioning, ML)."""

import pytest

from src.core.config import BotConfig, LearningConfig
from src.core.database import Database
from src.core.enums import BotMode, ProposalStatus
from src.learning.analyzer import PostTradeAnalyzer
from src.learning.backtester import WalkForwardBacktester, compute_performance_metrics
from src.learning.ml_model import ProfitProbabilityModel
from src.learning.optimizer import ParameterOptimizer
from src.learning.versioning import ConfigVersionManager


def _make_sample_trades(n: int = 35) -> list[dict]:
    """
    Generate a realistic synthetic dataset of `n` chronological closed trades where:
    - Trades with marginal score (0.65 - 0.68) lose -0.05 SOL (weak_entry_score pattern).
    - Trades with strong score (>= 0.72) win +0.08 SOL.
    Raising `scoring.threshold` from 0.65 -> 0.715 filters out the marginal losers
    across every walk-forward fold and in out-of-sample!
    """
    trades = []
    for i in range(n):
        is_marginal_loser = (i % 3 == 0)
        if is_marginal_loser:
            feats = {
                "price_momentum": 0.66,
                "volume_liquidity_ratio": 0.66,
                "holder_growth": 0.66,
                "buy_sell_ratio": 0.66,
                "token_age": 0.66,
            }
            score = 0.66
            pnl_sol = -0.05
            pnl_pct = -25.0
            exit_reason = "stop_loss"
        else:
            feats = {
                "price_momentum": 0.82,
                "volume_liquidity_ratio": 0.80,
                "holder_growth": 0.78,
                "buy_sell_ratio": 0.80,
                "token_age": 0.80,
            }
            score = 0.80
            pnl_sol = 0.08
            pnl_pct = 40.0
            exit_reason = "tp1"

        trades.append(
            {
                "id": i + 1,
                "entry_score": score,
                "entry_amount_sol": 0.2,
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "entry_slippage_pct": 0.5,
                "exit_slippage_pct": 0.5,
                "exit_reason": exit_reason,
                "holding_duration_sec": 900,
                "entry_features_json": {
                    "features": feats,
                    "raw": {"top10_holder_pct": 25.0, "token_age_seconds": 400},
                },
            }
        )
    return trades


# ---------------------------------------------------------------------------
# 1. Performance Metrics Calculation
# ---------------------------------------------------------------------------
def test_compute_performance_metrics():
    trades = [
        {"pnl_sol": 0.20},
        {"pnl_sol": -0.10},
        {"pnl_sol": 0.30},
        {"pnl_sol": -0.05},
    ]
    m = compute_performance_metrics(trades, initial_capital_sol=10.0)
    assert m.trades_count == 4
    assert m.wins == 2
    assert m.losses == 2
    assert m.win_rate == pytest.approx(50.0)
    assert m.net_pnl_sol == pytest.approx(0.35)
    assert m.expectancy == pytest.approx(0.0875)
    # Gross profit = 0.50, Gross loss = 0.15 -> PF = 3.3333
    assert m.profit_factor == pytest.approx(3.3333, rel=1e-3)
    assert m.max_drawdown_pct > 0.0


# ---------------------------------------------------------------------------
# 2. Optimizer Guardrails (Protected Params, Single Change, Max 10% Step)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_optimizer_blocks_protected_params_and_multi_changes():
    db = Database(db_path=":memory:")
    await db.connect()

    opt = ParameterOptimizer(config=BotConfig(), database=db)
    cur = opt.get_current_tunable_params()

    # Forbidden: risk or screening parameters
    with pytest.raises(ValueError, match="Forbidden parameter"):
        opt.validate_change_constraints(cur, {"risk.daily_loss_limit_sol": 10.0})

    with pytest.raises(ValueError, match="Forbidden parameter"):
        opt.validate_change_constraints(cur, {"screening.top10_holder_max_pct": 80.0})

    # Forbidden: more than 1 parameter per cycle
    with pytest.raises(ValueError, match="Maximum 1 parameter"):
        opt.validate_change_constraints(
            cur,
            {"scoring.threshold": 0.70, "position.max_position_pct": 1.5},
        )

    # Step clamping: try to jump threshold from 0.65 to 0.99 -> clamped to +10% (0.65 * 1.10 = 0.715)
    clamped = opt.validate_change_constraints(cur, {"scoring.threshold": 0.99})
    assert clamped["scoring.threshold"] == pytest.approx(0.715)

    await db.close()


# ---------------------------------------------------------------------------
# 3. Analyzer + Walk-Forward Validation + Manual Approval Flow
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_end_to_end_proposal_validation_and_manual_approval():
    db = Database(db_path=":memory:")
    await db.connect()

    config = BotConfig(
        mode=BotMode.PAPER,
        learning=LearningConfig(
            min_trades_for_proposal=20,
            max_param_change_pct=10.0,
            auto_apply_in_paper=False,
            require_manual_approval=True,
            walk_forward_splits=5,
            out_of_sample_pct=30.0,
            min_sample_trades=30,
        ),
    )
    opt = ParameterOptimizer(config=config, database=db)
    trades = _make_sample_trades(36)

    report, proposal, validation = await opt.generate_proposal_from_trades(trades)

    assert report.dominant_pattern is not None
    assert report.dominant_pattern.pattern_id == "weak_entry_score"
    assert proposal is not None
    assert validation is not None
    assert validation.passed is True
    assert validation.splits_improved == 5
    # Default mode: requires manual approval -> status is PENDING
    assert proposal.status == ProposalStatus.PENDING

    pending = await db.get_pending_proposals()
    assert len(pending) == 1
    prop_id = pending[0]["id"]

    # Manually approve proposal -> activates v1.1
    new_ver_tag = await opt.approve_proposal(prop_id)
    assert new_ver_tag == "v1.1"

    active_ver = await db.get_active_config_version()
    assert active_ver is not None
    assert active_ver["version_tag"] == "v1.1"

    await db.close()


# ---------------------------------------------------------------------------
# 4. Versioning & Automatic Rollback When Performance Degrades
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_config_versioning_auto_rollback_on_degradation():
    db = Database(db_path=":memory:")
    await db.connect()

    vm = ConfigVersionManager(database=db)
    await vm.create_initial_version({"scoring.threshold": 0.65}, version_tag="v1.0")

    # Record strong metrics on v1.0
    good_trades = [{"pnl_sol": 0.05} for _ in range(12)]
    await vm.update_active_version_metrics(good_trades)

    # Activate v1.1
    ver_tag = await vm.activate_new_version(
        new_params={"scoring.threshold": 0.70},
        change_description="Test update",
    )
    assert ver_tag == "v1.1"

    # Simulate poor trades under v1.1 (12 losing trades)
    bad_trades = [{"pnl_sol": -0.04} for _ in range(12)]
    rolled_back, restored, reason = await vm.check_and_rollback_if_degraded(
        recent_trades=bad_trades,
        min_eval_trades=10,
    )

    assert rolled_back is True
    assert restored is not None
    assert restored["scoring.threshold"] == pytest.approx(0.65)
    assert "Auto-rollback triggered" in reason

    active_now = await db.get_active_config_version()
    assert active_now is not None
    assert active_now["version_tag"] == "v1.0"

    await db.close()


# ---------------------------------------------------------------------------
# 5. Optional ML Model (Time-Based Split & Overfitting Guard)
# ---------------------------------------------------------------------------
def test_ml_model_time_split_and_overfit_guard():
    trades = _make_sample_trades(40)
    ml = ProfitProbabilityModel(min_samples=30, train_ratio=0.70, max_overfit_gap=0.15)

    report = ml.train_time_split(trades)
    assert report.trained is True
    assert report.train_size == 28
    assert report.val_size == 12
    assert report.is_overfitted is False
    assert report.model_active is True

    # High feature vector should predict > 0.50 profit probability
    p_high = ml.predict_proba(
        {
            "price_momentum": 0.9,
            "volume_liquidity_ratio": 0.9,
            "holder_growth": 0.9,
            "buy_sell_ratio": 0.9,
            "token_age": 0.9,
        }
    )
    assert 0.5 < p_high <= 1.0

