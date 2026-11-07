"""ParameterOptimizer — generates bounded, single-parameter proposals validated via walk-forward backtest."""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from src.core.config import BotConfig
from src.core.database import Database
from src.core.enums import BotMode, ProposalStatus
from src.core.models import OptimizationProposal, utc_now_iso
from src.learning.analyzer import AnalysisReport, PostTradeAnalyzer
from src.learning.backtester import BacktestValidationResult, WalkForwardBacktester
from src.learning.versioning import ConfigVersionManager
from src.risk.limits import is_protected_parameter

logger = logging.getLogger(__name__)

# Hard min/max bounds for every tunable parameter.
# Risk & Screening parameters are intentionally EXCLUDED and forbidden.
PARAMETER_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    "scoring.threshold": (0.50, 0.85),
    "scoring.weights.price_momentum": (0.10, 0.40),
    "scoring.weights.volume_liquidity_ratio": (0.10, 0.35),
    "scoring.weights.holder_growth": (0.10, 0.35),
    "scoring.weights.buy_sell_ratio": (0.10, 0.35),
    "scoring.weights.token_age": (0.05, 0.25),
    "position.stop_loss_pct": (-40.0, -15.0),
    "position.max_position_pct": (0.5, 3.0),
}


class ParameterOptimizer:
    """
    Controlled self-learning parameter optimizer:
    - NEVER modifies source code, risk limits (`risk.*`), or security filters (`screening.*`).
    - Proposes at most ONE small parameter change per cycle (<= `max_param_change_pct`).
    - Enforces min/max bounds (`PARAMETER_BOUNDS`).
    - Validates every proposal with `WalkForwardBacktester` (walk-forward + out-of-sample).
    - Default mode requires manual human approval (`status='pending'`).
    """

    def __init__(
        self,
        config: BotConfig,
        database: Database,
        analyzer: PostTradeAnalyzer | None = None,
        backtester: WalkForwardBacktester | None = None,
        version_manager: ConfigVersionManager | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.analyzer = analyzer or PostTradeAnalyzer()
        self.backtester = backtester or WalkForwardBacktester(config.learning)
        self.version_manager = version_manager or ConfigVersionManager(database)

    def _apply_param_to_config(self, param: str, val: Any) -> None:
        """Apply a single tunable scalar parameter into runtime `self.config`."""
        if not isinstance(val, (int, float)):
            return
        fval = float(val)
        if param == "scoring.threshold":
            object.__setattr__(self.config.scoring, "threshold", fval)
        elif param.startswith("scoring.weights."):
            w_key = param.split("scoring.weights.", 1)[1]
            new_weights = dict(self.config.scoring.weights)
            new_weights[w_key] = fval
            object.__setattr__(self.config.scoring, "weights", new_weights)
        elif param == "position.stop_loss_pct":
            object.__setattr__(self.config.position, "stop_loss_pct", fval)
        elif param == "position.max_position_pct":
            object.__setattr__(self.config.position, "max_position_pct", fval)

    def get_current_tunable_params(self) -> dict[str, Any]:
        """Return a flat dictionary of currently configured tunable parameters."""
        w = dict(self.config.scoring.weights)
        return {
            "scoring.threshold": self.config.scoring.threshold,
            "scoring.weights": w,
            "scoring.weights.price_momentum": w.get("price_momentum", 0.25),
            "scoring.weights.volume_liquidity_ratio": w.get("volume_liquidity_ratio", 0.20),
            "scoring.weights.holder_growth": w.get("holder_growth", 0.20),
            "scoring.weights.buy_sell_ratio": w.get("buy_sell_ratio", 0.20),
            "scoring.weights.token_age": w.get("token_age", 0.15),
            "position.stop_loss_pct": self.config.position.stop_loss_pct,
            "position.max_position_pct": self.config.position.max_position_pct,
        }

    def validate_change_constraints(
        self,
        current_params: dict[str, Any],
        proposed_changes: dict[str, float],
    ) -> dict[str, float]:
        """
        Enforce strict guardrails on `proposed_changes`:
        1. At most 1 parameter may be changed per cycle.
        2. Parameter must not be protected (`risk.*`, `screening.*`, etc.) and must be in `PARAMETER_BOUNDS`.
        3. Relative step size must not exceed `max_param_change_pct`, and final value is clamped to `[min, max]`.
        """
        if not proposed_changes:
            raise ValueError("proposed_changes cannot be empty")
        if len(proposed_changes) > 1:
            raise ValueError(
                f"Maximum 1 parameter change allowed per cycle, got {len(proposed_changes)}"
            )

        param_name, target_val = next(iter(proposed_changes.items()))

        if is_protected_parameter(param_name) or param_name not in PARAMETER_BOUNDS:
            raise ValueError(
                f"Forbidden parameter modification '{param_name}': risk and security filters cannot be modified"
            )

        low_bound, high_bound = PARAMETER_BOUNDS[param_name]
        cur_val = float(current_params.get(param_name, (low_bound + high_bound) / 2.0))

        # Clamp step size to max_param_change_pct of current magnitude
        max_step = abs(cur_val) * (self.config.learning.max_param_change_pct / 100.0)
        delta = float(target_val) - cur_val
        if abs(delta) > max_step + 1e-9:
            delta = max_step if delta > 0 else -max_step

        bounded_val = max(low_bound, min(high_bound, cur_val + delta))
        return {param_name: round(bounded_val, 4)}

    async def generate_proposal_from_trades(
        self,
        trades: list[dict[str, Any]],
        period_label: str = "last_24h",
        current_params_override: dict[str, Any] | None = None,
    ) -> tuple[AnalysisReport, OptimizationProposal | None, BacktestValidationResult | None]:
        """
        Run post-trade analysis on `trades`, craft a single bounded parameter adjustment
        addressing the dominant loss pattern, validate it via walk-forward backtest,
        and store the proposal in SQLite.
        """
        if current_params_override is not None:
            current_params = current_params_override
        else:
            current_params = self.get_current_tunable_params()
            active_ver = await self.db.get_active_config_version()
            if active_ver and active_ver.get("config_snapshot"):
                try:
                    snap = json.loads(active_ver["config_snapshot"])
                    if isinstance(snap, dict):
                        current_params.update(snap)
                except json.JSONDecodeError:
                    pass

        await self.version_manager.create_initial_version(current_params)

        report = self.analyzer.analyze_trades(
            trades=trades,
            current_threshold=float(current_params.get("scoring.threshold", 0.65)),
            period_label=period_label,
        )

        if len(trades) < self.config.learning.min_trades_for_proposal:
            logger.info(
                "Skipping proposal generation: %d trades < min_trades_for_proposal (%d)",
                len(trades), self.config.learning.min_trades_for_proposal,
            )
            return report, None, None

        if report.dominant_pattern is None:
            return report, None, None

        dom = report.dominant_pattern
        param = dom.suggested_param
        cur_val = float(current_params.get(param, 0.5))
        step_pct = self.config.learning.max_param_change_pct / 100.0

        # Propose a single step in the suggested direction
        if dom.suggested_direction == "increase":
            raw_target = cur_val + abs(cur_val) * step_pct
        else:
            raw_target = cur_val - abs(cur_val) * step_pct

        bounded_change = self.validate_change_constraints(current_params, {param: raw_target})
        if abs(bounded_change[param] - cur_val) < 1e-6:
            return report, None, None

        # Avoid inserting duplicate pending proposals for the exact same parameter change
        pending_list = await self.db.get_pending_proposals()
        for pend in pending_list:
            try:
                p_chg = json.loads(pend.get("proposed_changes") or "{}")
                if (
                    p_chg.get("parameter") == param
                    and abs(float(p_chg.get("new_value", -999)) - bounded_change[param]) < 1e-6
                ):
                    return report, None, None
            except (ValueError, TypeError):
                pass

        # Validate on walk-forward + out-of-sample backtest
        validation = self.backtester.validate_proposal(
            historical_trades=trades,
            baseline_params=current_params,
            proposed_changes=bounded_change,
        )

        # Determine initial status (manual approval by default)
        can_auto_apply = (
            validation.passed
            and not self.config.learning.require_manual_approval
            and self.config.mode == BotMode.PAPER
            and self.config.learning.auto_apply_in_paper
        )

        status = ProposalStatus.AUTO_APPLIED if can_auto_apply else ProposalStatus.PENDING
        decided_by = "auto_paper" if can_auto_apply else None

        proposal = OptimizationProposal(
            analysis_period=period_label,
            trades_analyzed=len(trades),
            patterns_found=[p.pattern_id for p in report.patterns],
            proposed_changes={
                "parameter": param,
                "old_value": cur_val,
                "new_value": bounded_change[param],
            },
            rationale=f"{dom.description} ({dom.count} losses). {validation.reason}",
            backtest_result=validation.to_dict(),
            backtest_win_rate=validation.proposed_metrics.win_rate,
            backtest_expectancy=validation.proposed_metrics.expectancy,
            backtest_improvement_pct=validation.improvement_pct,
            sample_size=validation.sample_size,
            passed_validation=validation.passed,
            status=status,
            decided_by=decided_by,
        )

        proposal_id = await self.db.insert_proposal(proposal)

        if can_auto_apply:
            updated_params = dict(current_params)
            updated_params.update(bounded_change)
            self._apply_param_to_config(param, bounded_change[param])
            ver_tag = await self.version_manager.activate_new_version(
                new_params=updated_params,
                change_description=f"Auto-applied proposal #{proposal_id}: {param}={bounded_change[param]}",
                change_source="optimizer_proposal",
                proposal_id=proposal_id,
            )
            proposal.applied_version = ver_tag

        return report, proposal, validation

    async def approve_proposal(
        self,
        proposal_id: int,
        current_params: dict[str, Any] | None = None,
    ) -> str:
        """
        Manually approve a pending proposal that passed validation, creating a new active
        ConfigVersion in SQLite. Returns the new `version_tag`.
        """
        cursor = await self.db.conn.execute(
            "SELECT * FROM optimization_proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"Proposal #{proposal_id} not found")

        prop_dict = dict(row)
        if not prop_dict.get("passed_validation"):
            raise ValueError(f"Cannot approve proposal #{proposal_id}: failed walk-forward validation")

        changes_blob = json.loads(prop_dict["proposed_changes"])
        param = changes_blob["parameter"]
        new_val = float(changes_blob["new_value"])

        base_params = dict(current_params or self.get_current_tunable_params())
        checked = self.validate_change_constraints(base_params, {param: new_val})
        base_params.update(checked)

        ver_tag = await self.version_manager.activate_new_version(
            new_params=base_params,
            change_description=f"Manual approval of proposal #{proposal_id}: {param}={checked[param]}",
            change_source="manual",
            proposal_id=proposal_id,
        )

        await self.db.conn.execute(
            """
            UPDATE optimization_proposals
            SET status = ?, decided_by = 'manual', applied_version = ?, decided_at = ?
            WHERE id = ?
            """,
            (ProposalStatus.APPROVED.value, ver_tag, utc_now_iso(), proposal_id),
        )
        await self.db.conn.commit()
        return ver_tag

