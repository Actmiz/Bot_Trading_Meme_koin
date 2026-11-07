"""Walk-forward backtester and performance metrics calculator (win rate, expectancy, max drawdown, profit factor)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from src.core.config import LearningConfig


@dataclass(frozen=True)
class PerformanceMetrics:
    """Core trading performance metrics."""

    trades_count: int
    wins: int
    losses: int
    win_rate: float          # Percentage [0..100]
    expectancy: float        # Average net PnL (SOL) per trade
    profit_factor: float     # Gross profit / Gross loss
    net_pnl_sol: float
    max_drawdown_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_performance_metrics(
    trades: list[dict[str, Any]],
    initial_capital_sol: float = 10.0,
) -> PerformanceMetrics:
    """
    Compute win rate, expectancy, profit factor, net PnL, and max drawdown (%)
    from a chronological list of closed trade dicts.
    """
    if not trades:
        return PerformanceMetrics(
            trades_count=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            expectancy=0.0,
            profit_factor=0.0,
            net_pnl_sol=0.0,
            max_drawdown_pct=0.0,
        )

    pnls = [float(t.get("pnl_sol") or 0.0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    count = len(pnls)

    win_rate = (wins / count) * 100.0
    net_pnl = sum(pnls)
    expectancy = net_pnl / count

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss > 1e-9:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 99.99 if gross_profit > 0 else 0.0

    # Equity curve & max drawdown calculation
    equity = max(0.01, initial_capital_sol)
    peak = equity
    max_dd_pct = 0.0

    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        if peak > 0:
            dd_pct = ((peak - equity) / peak) * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    return PerformanceMetrics(
        trades_count=count,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 2),
        expectancy=round(expectancy, 6),
        profit_factor=round(profit_factor, 4),
        net_pnl_sol=round(net_pnl, 6),
        max_drawdown_pct=round(max_dd_pct, 2),
    )


@dataclass(frozen=True)
class BacktestValidationResult:
    """Result of walk-forward out-of-sample validation for a proposed parameter change."""

    passed: bool
    sample_size: int
    baseline_metrics: PerformanceMetrics
    proposed_metrics: PerformanceMetrics
    improvement_pct: float
    splits_improved: int
    total_splits: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "sample_size": self.sample_size,
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "proposed_metrics": self.proposed_metrics.to_dict(),
            "improvement_pct": self.improvement_pct,
            "splits_improved": self.splits_improved,
            "total_splits": self.total_splits,
            "reason": self.reason,
        }


def _extract_features_from_trade(trade: dict[str, Any]) -> dict[str, float]:
    """Parse normalized feature dictionary stored in trade['entry_features_json']."""
    raw = trade.get("entry_features_json")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, dict):
        if "features" in raw and isinstance(raw["features"], dict):
            return {k: float(v or 0.0) for k, v in raw["features"].items()}
        return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    return {}


class WalkForwardBacktester:
    """
    Validates proposed parameter changes on historical trades using:
    1. Out-of-sample split (`out_of_sample_pct`).
    2. Walk-forward consistency across `walk_forward_splits` chronological folds.
    """

    def __init__(self, config: LearningConfig, initial_capital_sol: float = 10.0) -> None:
        self.config = config
        self.initial_capital_sol = initial_capital_sol

    def simulate_trades_with_params(
        self,
        trades: list[dict[str, Any]],
        baseline_params: dict[str, Any],
        candidate_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Replay a sequence of historical trades under `candidate_params`:
        - Recomputes entry score if `scoring.weights.*` or `scoring.threshold` changed,
          filtering out trades that would not have passed the candidate threshold.
        - Adjusts stop-loss outcome if `position.stop_loss_pct` changed.
        - Scales PnL proportionally if `position.max_position_pct` changed.
        """
        base_threshold = float(baseline_params.get("scoring.threshold", 0.65))
        cand_threshold = float(candidate_params.get("scoring.threshold", base_threshold))

        base_weights: dict[str, float] = dict(
            baseline_params.get(
                "scoring.weights",
                {
                    "price_momentum": 0.25,
                    "volume_liquidity_ratio": 0.20,
                    "holder_growth": 0.20,
                    "buy_sell_ratio": 0.20,
                    "token_age": 0.15,
                },
            )
        )
        cand_weights = dict(base_weights)
        for k, v in candidate_params.items():
            if k.startswith("scoring.weights."):
                feat_key = k.split("scoring.weights.", 1)[1]
                cand_weights[feat_key] = float(v)

        # Normalize candidate weights if any weight changed
        w_sum = sum(cand_weights.values())
        if w_sum > 0:
            cand_weights = {k: v / w_sum for k, v in cand_weights.items()}

        base_sl = float(baseline_params.get("position.stop_loss_pct", -30.0))
        cand_sl = float(candidate_params.get("position.stop_loss_pct", base_sl))

        base_pos_pct = float(baseline_params.get("position.max_position_pct", 2.0))
        cand_pos_pct = float(candidate_params.get("position.max_position_pct", base_pos_pct))
        size_scale = (cand_pos_pct / base_pos_pct) if base_pos_pct > 0 else 1.0

        replayed: list[dict[str, Any]] = []
        for t in trades:
            feats = _extract_features_from_trade(t)
            if feats:
                score = sum(cand_weights.get(k, 0.0) * feats.get(k, 0.5) for k in cand_weights)
            else:
                score = float(t.get("entry_score") or base_threshold)

            # Filter out trade if score is below candidate threshold
            if score < cand_threshold:
                continue

            pnl_sol = float(t.get("pnl_sol") or 0.0)
            pnl_pct = float(t.get("pnl_pct") or 0.0)
            entry_sol = float(t.get("entry_amount_sol") or 0.1)

            # If candidate stop-loss is tighter (e.g. -20% vs -30%) and trade lost worse than cand_sl,
            # cap the loss at cand_sl
            if pnl_pct < cand_sl < 0:
                pnl_pct = cand_sl
                pnl_sol = entry_sol * (cand_sl / 100.0)

            scaled_pnl_sol = pnl_sol * size_scale
            replayed_trade = dict(t)
            replayed_trade["pnl_sol"] = scaled_pnl_sol
            replayed_trade["pnl_pct"] = pnl_pct
            replayed_trade["entry_score"] = round(score, 4)
            replayed.append(replayed_trade)

        return replayed

    def validate_proposal(
        self,
        historical_trades: list[dict[str, Any]],
        baseline_params: dict[str, Any],
        proposed_changes: dict[str, Any],
    ) -> BacktestValidationResult:
        """
        Validate `proposed_changes` using walk-forward splits and out-of-sample evaluation.
        """
        total_n = len(historical_trades)
        if total_n < self.config.min_sample_trades:
            empty_m = compute_performance_metrics([])
            return BacktestValidationResult(
                passed=False,
                sample_size=total_n,
                baseline_metrics=empty_m,
                proposed_metrics=empty_m,
                improvement_pct=0.0,
                splits_improved=0,
                total_splits=self.config.walk_forward_splits,
                reason=(
                    f"Insufficient sample size: {total_n} trades < minimum "
                    f"{self.config.min_sample_trades}"
                ),
            )

        candidate_params = dict(baseline_params)
        candidate_params.update(proposed_changes)

        # 1. Walk-forward consistency across K chronological folds
        k_splits = max(2, self.config.walk_forward_splits)
        fold_size = max(1, total_n // k_splits)
        splits_improved = 0

        for i in range(k_splits):
            start_idx = i * fold_size
            end_idx = total_n if i == k_splits - 1 else (i + 1) * fold_size
            fold_trades = historical_trades[start_idx:end_idx]
            if not fold_trades:
                continue

            base_fold = self.simulate_trades_with_params(fold_trades, baseline_params, baseline_params)
            cand_fold = self.simulate_trades_with_params(fold_trades, baseline_params, candidate_params)

            base_m = compute_performance_metrics(base_fold, self.initial_capital_sol)
            cand_m = compute_performance_metrics(cand_fold, self.initial_capital_sol)

            if cand_m.expectancy > base_m.expectancy or cand_m.net_pnl_sol > base_m.net_pnl_sol:
                splits_improved += 1

        # 2. Out-of-sample holdout evaluation (last out_of_sample_pct% of trades)
        oos_count = max(5, int(total_n * (self.config.out_of_sample_pct / 100.0)))
        oos_trades = historical_trades[-oos_count:]

        base_oos = self.simulate_trades_with_params(oos_trades, baseline_params, baseline_params)
        cand_oos = self.simulate_trades_with_params(oos_trades, baseline_params, candidate_params)

        base_oos_m = compute_performance_metrics(base_oos, self.initial_capital_sol)
        cand_oos_m = compute_performance_metrics(cand_oos, self.initial_capital_sol)

        # Calculate expectancy improvement
        denom = abs(base_oos_m.expectancy) if abs(base_oos_m.expectancy) > 1e-6 else 0.01
        improvement_pct = round(
            ((cand_oos_m.expectancy - base_oos_m.expectancy) / denom) * 100.0, 2
        )

        # Pass criteria:
        # - Majority of walk-forward folds improved
        # - Out-of-sample expectancy strictly improved
        # - Out-of-sample max drawdown did not worsen beyond baseline + 2%
        majority_splits = (splits_improved >= (k_splits // 2) + 1)
        oos_better = cand_oos_m.expectancy > base_oos_m.expectancy
        dd_safe = cand_oos_m.max_drawdown_pct <= (base_oos_m.max_drawdown_pct + 2.0)

        passed = majority_splits and oos_better and dd_safe
        if passed:
            reason = (
                f"Passed walk-forward ({splits_improved}/{k_splits} splits improved) "
                f"and OOS expectancy improved by {improvement_pct:+.1f}%"
            )
        else:
            reason = (
                f"Rejected by validation: splits={splits_improved}/{k_splits}, "
                f"OOS expectancy {base_oos_m.expectancy:+.4f} -> {cand_oos_m.expectancy:+.4f} SOL, "
                f"OOS maxDD {base_oos_m.max_drawdown_pct:.1f}% -> {cand_oos_m.max_drawdown_pct:.1f}%"
            )

        return BacktestValidationResult(
            passed=passed,
            sample_size=total_n,
            baseline_metrics=base_oos_m,
            proposed_metrics=cand_oos_m,
            improvement_pct=improvement_pct,
            splits_improved=splits_improved,
            total_splits=k_splits,
            reason=reason,
        )

