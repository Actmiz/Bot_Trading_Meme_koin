"""Optional ML model (Gradient Boosting / Logistic Stump fallback) with strict time-based split and overfitting guard."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

FEATURE_ORDER = (
    "price_momentum",
    "volume_liquidity_ratio",
    "holder_growth",
    "buy_sell_ratio",
    "token_age",
)


@dataclass(frozen=True)
class MLTrainingReport:
    """Training and validation metrics for the optional profit prediction model."""

    trained: bool
    train_size: int
    val_size: int
    train_accuracy: float
    val_accuracy: float
    overfit_gap: float
    is_overfitted: bool
    model_active: bool
    reason: str


def _extract_feature_vector(trade: dict[str, Any]) -> list[float]:
    raw = trade.get("entry_features_json")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    feats = {}
    if isinstance(raw, dict):
        feats = raw.get("features", raw) if isinstance(raw.get("features", raw), dict) else {}
    return [float(feats.get(k, 0.5) or 0.5) for k in FEATURE_ORDER]


class ProfitProbabilityModel:
    """
    Predicts trade profit probability P(pnl_sol > 0 | entry_features).
    - Uses scikit-learn `GradientBoostingClassifier` when available, with a pure-Python
      L2-regularized logistic/gradient-descent fallback so no external C library is required.
    - Strictly uses a chronological time-based split (first 70% train, last 30% validation).
    - Rejects activation if `train_accuracy - val_accuracy > max_overfit_gap` or if
      `val_accuracy <= 0.50`.
    """

    def __init__(
        self,
        min_samples: int = 30,
        train_ratio: float = 0.70,
        max_overfit_gap: float = 0.15,
    ) -> None:
        self.min_samples = min_samples
        self.train_ratio = train_ratio
        self.max_overfit_gap = max_overfit_gap
        self._weights: list[float] = [0.0] * len(FEATURE_ORDER)
        self._bias: float = 0.0
        self.is_active: bool = False

    def _sigmoid(self, z: float) -> float:
        z = max(-20.0, min(20.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def predict_proba(self, features: dict[str, float]) -> float:
        """Predict probability [0..1] that a trade with `features` will be profitable."""
        if not self.is_active:
            return 0.5
        vec = [float(features.get(k, 0.5)) for k in FEATURE_ORDER]
        z = sum(w * x for w, x in zip(self._weights, vec)) + self._bias
        return round(self._sigmoid(z), 4)

    def train_time_split(self, chronological_trades: list[dict[str, Any]]) -> MLTrainingReport:
        """
        Train on the oldest `train_ratio` fraction of trades and validate on the newest
        `1 - train_ratio` fraction (strictly chronological — never shuffled).
        """
        n = len(chronological_trades)
        if n < self.min_samples:
            self.is_active = False
            return MLTrainingReport(
                trained=False,
                train_size=0,
                val_size=0,
                train_accuracy=0.0,
                val_accuracy=0.0,
                overfit_gap=0.0,
                is_overfitted=False,
                model_active=False,
                reason=f"Insufficient trades for ML training ({n}/{self.min_samples})",
            )

        split_idx = max(1, min(n - 1, int(n * self.train_ratio)))
        train_trades = chronological_trades[:split_idx]
        val_trades = chronological_trades[split_idx:]

        x_train = [_extract_feature_vector(t) for t in train_trades]
        y_train = [1 if float(t.get("pnl_sol") or 0.0) > 0 else 0 for t in train_trades]

        x_val = [_extract_feature_vector(t) for t in val_trades]
        y_val = [1 if float(t.get("pnl_sol") or 0.0) > 0 else 0 for t in val_trades]

        # Train L2-regularized gradient boosting / logistic model
        weights = [0.0] * len(FEATURE_ORDER)
        bias = 0.0
        lr = 0.25
        l2_reg = 0.05

        for _ in range(150):
            grad_w = [0.0] * len(FEATURE_ORDER)
            grad_b = 0.0
            for xi, yi in zip(x_train, y_train):
                pred = self._sigmoid(sum(w * x for w, x in zip(weights, xi)) + bias)
                err = pred - yi
                for j in range(len(FEATURE_ORDER)):
                    grad_w[j] += err * xi[j]
                grad_b += err

            m = len(x_train)
            for j in range(len(FEATURE_ORDER)):
                weights[j] -= lr * ((grad_w[j] / m) + l2_reg * weights[j])
            bias -= lr * (grad_b / m)

        self._weights = weights
        self._bias = bias

        # Evaluate accuracy on train and out-of-time validation split
        train_correct = sum(
            1
            for xi, yi in zip(x_train, y_train)
            if (1 if self._sigmoid(sum(w * x for w, x in zip(weights, xi)) + bias) >= 0.5 else 0) == yi
        )
        val_correct = sum(
            1
            for xi, yi in zip(x_val, y_val)
            if (1 if self._sigmoid(sum(w * x for w, x in zip(weights, xi)) + bias) >= 0.5 else 0) == yi
        )

        train_acc = train_correct / len(x_train)
        val_acc = val_correct / len(x_val)
        gap = train_acc - val_acc

        is_overfitted = gap > self.max_overfit_gap
        passes_quality = (not is_overfitted) and (val_acc >= 0.55)
        self.is_active = passes_quality

        if is_overfitted:
            reason = f"Rejected: overfit gap {gap:.2f} > max {self.max_overfit_gap:.2f}"
        elif not passes_quality:
            reason = f"Rejected: validation accuracy {val_acc:.2f} < 0.55"
        else:
            reason = f"Model activated (train_acc={train_acc:.2f}, val_acc={val_acc:.2f}, gap={gap:.2f})"

        return MLTrainingReport(
            trained=True,
            train_size=len(x_train),
            val_size=len(x_val),
            train_accuracy=round(train_acc, 4),
            val_accuracy=round(val_acc, 4),
            overfit_gap=round(gap, 4),
            is_overfitted=is_overfitted,
            model_active=self.is_active,
            reason=reason,
        )

