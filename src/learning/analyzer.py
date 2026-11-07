"""PostTradeAnalyzer — groups losing trades and identifies recurring loss patterns."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.learning.backtester import PerformanceMetrics, compute_performance_metrics


@dataclass(frozen=True)
class LossPattern:
    """A detected recurring pattern among losing trades."""

    pattern_id: str
    description: str
    count: int
    share_of_losses_pct: float
    avg_loss_sol: float
    suggested_param: str
    suggested_direction: str  # 'increase' | 'decrease'


@dataclass(frozen=True)
class AnalysisReport:
    """Periodic post-trade analysis report."""

    period_label: str
    total_trades: int
    losing_trades_count: int
    metrics: PerformanceMetrics
    patterns: list[LossPattern] = field(default_factory=list)
    dominant_pattern: LossPattern | None = None
    summary_text: str = ""


def _parse_entry_blob(trade: dict[str, Any]) -> dict[str, Any]:
    raw = trade.get("entry_features_json")
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


class PostTradeAnalyzer:
    """
    Analyzes closed trades to diagnose WHY losing trades lost money:
    1. `high_holder_concentration` — losses when top10_holder_pct is high / holder_growth is weak.
    2. `late_entry` — losses when entering old tokens (token_age_seconds > 1200 or low token_age score).
    3. `high_slippage` — losses where entry + exit slippage eroded the edge.
    4. `tight_stop_loss` — frequent rapid stop-loss exits.
    5. `weak_entry_score` — losses on marginal entry scores just above threshold.
    """

    def analyze_trades(
        self,
        trades: list[dict[str, Any]],
        current_threshold: float = 0.65,
        period_label: str = "last_24h",
        initial_capital_sol: float = 10.0,
    ) -> AnalysisReport:
        metrics = compute_performance_metrics(trades, initial_capital_sol=initial_capital_sol)
        losers = [t for t in trades if float(t.get("pnl_sol") or 0.0) <= 0.0]

        if not losers:
            return AnalysisReport(
                period_label=period_label,
                total_trades=len(trades),
                losing_trades_count=0,
                metrics=metrics,
                patterns=[],
                dominant_pattern=None,
                summary_text="No losing trades in period.",
            )

        buckets: dict[str, list[float]] = {
            "high_holder_concentration": [],
            "late_entry": [],
            "high_slippage": [],
            "tight_stop_loss": [],
            "weak_entry_score": [],
        }

        for t in losers:
            loss_sol = float(t.get("pnl_sol") or 0.0)
            blob = _parse_entry_blob(t)
            feats = blob.get("features", {}) if isinstance(blob.get("features"), dict) else {}
            raw = blob.get("raw", {}) if isinstance(blob.get("raw"), dict) else {}

            # 1. Holder concentration check
            top10_pct = float(raw.get("top10_holder_pct") or 0.0)
            feat_holder = float(feats.get("holder_growth", 0.5))
            if top10_pct >= 35.0 or feat_holder < 0.45:
                buckets["high_holder_concentration"].append(loss_sol)

            # 2. Late entry check
            age_sec = float(raw.get("token_age_seconds") or 0.0)
            feat_age = float(feats.get("token_age", 0.5))
            if age_sec > 1200 or feat_age < 0.45:
                buckets["late_entry"].append(loss_sol)

            # 3. High slippage check
            entry_slip = float(t.get("entry_slippage_pct") or 0.0)
            exit_slip = float(t.get("exit_slippage_pct") or 0.0)
            if (entry_slip + exit_slip) >= 2.5:
                buckets["high_slippage"].append(loss_sol)

            # 4. Stop-loss exit check
            exit_reason = str(t.get("exit_reason") or "")
            hold_sec = int(t.get("holding_duration_sec") or 9999)
            if exit_reason == "stop_loss" and hold_sec <= 600:
                buckets["tight_stop_loss"].append(loss_sol)

            # 5. Marginal score check
            entry_score = float(t.get("entry_score") or current_threshold)
            if entry_score < (current_threshold + 0.07):
                buckets["weak_entry_score"].append(loss_sol)

        pattern_defs = {
            "weak_entry_score": (
                "Marginal entry score close to threshold",
                "scoring.threshold",
                "increase",
            ),
            "high_holder_concentration": (
                "Losses on tokens with weak holder breadth / high top-10 concentration",
                "scoring.weights.holder_growth",
                "increase",
            ),
            "late_entry": (
                "Losses from entering tokens too late in their lifecycle",
                "scoring.weights.token_age",
                "increase",
            ),
            "high_slippage": (
                "Losses exacerbated by high entry/exit slippage on illiquid pools",
                "position.max_position_pct",
                "decrease",
            ),
            "tight_stop_loss": (
                "Rapid stop-outs within 10 minutes of entry due to meme volatility",
                "position.stop_loss_pct",
                "decrease",  # e.g. from -25% to -27.5% (wider stop) or tighten threshold
            ),
        }

        detected_patterns: list[LossPattern] = []
        num_losers = len(losers)

        for pid, losses_list in buckets.items():
            if not losses_list:
                continue
            desc, param, direction = pattern_defs[pid]
            share_pct = (len(losses_list) / num_losers) * 100.0
            avg_loss = sum(losses_list) / len(losses_list)
            detected_patterns.append(
                LossPattern(
                    pattern_id=pid,
                    description=desc,
                    count=len(losses_list),
                    share_of_losses_pct=round(share_pct, 1),
                    avg_loss_sol=round(avg_loss, 6),
                    suggested_param=param,
                    suggested_direction=direction,
                )
            )

        detected_patterns.sort(key=lambda p: (p.count, abs(p.avg_loss_sol)), reverse=True)
        dominant = detected_patterns[0] if detected_patterns else None

        summary = (
            f"Analyzed {len(trades)} trades ({num_losers} losses, win_rate={metrics.win_rate:.1f}%, "
            f"expectancy={metrics.expectancy:+.4f} SOL). "
        )
        if dominant:
            summary += (
                f"Dominant loss pattern: {dominant.pattern_id} "
                f"({dominant.count}/{num_losers} losses, {dominant.share_of_losses_pct:.1f}%)."
            )

        return AnalysisReport(
            period_label=period_label,
            total_trades=len(trades),
            losing_trades_count=num_losers,
            metrics=metrics,
            patterns=detected_patterns,
            dominant_pattern=dominant,
            summary_text=summary,
        )

