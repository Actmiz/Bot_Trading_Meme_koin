"""ConfigVersionManager — versions parameter snapshots in SQLite and performs automatic rollback on degradation."""

from __future__ import annotations

import json
import logging
from typing import Any

from src.core.database import Database
from src.core.enums import ProposalStatus
from src.core.models import ConfigVersion, utc_now_iso
from src.learning.backtester import PerformanceMetrics, compute_performance_metrics

logger = logging.getLogger(__name__)


class ConfigVersionManager:
    """
    Manages configuration versions in the `config_versions` table:
    - Records every parameter change with a unique `version_tag` (`v1.0`, `v1.1`, ...).
    - Tracks performance metrics achieved under each version.
    - Automatically rolls back to the previous known-good version if live/paper performance
      under the newly applied version degrades.
    """

    def __init__(self, database: Database) -> None:
        self.db = database

    async def create_initial_version(
        self,
        params_snapshot: dict[str, Any],
        version_tag: str = "v1.0",
    ) -> int:
        """Create and activate the baseline configuration version if none is active."""
        active = await self.db.get_active_config_version()
        if active is not None:
            return int(active["id"])

        cv = ConfigVersion(
            version_tag=version_tag,
            config_snapshot=params_snapshot,
            change_description="Initial baseline configuration",
            change_source="manual",
            is_active=True,
            activated_at=utc_now_iso(),
        )
        return await self.db.insert_config_version(cv)

    async def activate_new_version(
        self,
        new_params: dict[str, Any],
        change_description: str,
        change_source: str = "optimizer_proposal",
        proposal_id: int | None = None,
    ) -> str:
        """
        Deactivate the current active version and activate a new version snapshot.
        Returns the new `version_tag`.
        """
        now = utc_now_iso()
        cursor = await self.db.conn.execute("SELECT COUNT(*) FROM config_versions")
        row = await cursor.fetchone()
        next_num = (int(row[0]) if row else 0) + 1
        version_tag = f"v1.{next_num - 1}"

        # Deactivate currently active version
        await self.db.conn.execute(
            "UPDATE config_versions SET is_active = FALSE, deactivated_at = ? WHERE is_active = TRUE",
            (now,),
        )

        cv = ConfigVersion(
            version_tag=version_tag,
            config_snapshot=new_params,
            change_description=change_description,
            change_source=change_source,
            proposal_id=proposal_id,
            is_active=True,
            activated_at=now,
        )
        await self.db.insert_config_version(cv)
        logger.info("Activated config version %s (%s)", version_tag, change_description)
        return version_tag

    async def update_active_version_metrics(
        self,
        trades: list[dict[str, Any]],
        initial_capital_sol: float = 10.0,
    ) -> PerformanceMetrics:
        """Update performance metrics on the currently active config version."""
        metrics = compute_performance_metrics(trades, initial_capital_sol=initial_capital_sol)
        await self.db.conn.execute(
            """
            UPDATE config_versions SET
                trades_count = ?, win_rate = ?, expectancy = ?,
                profit_factor = ?, max_drawdown_pct = ?
            WHERE is_active = TRUE
            """,
            (
                metrics.trades_count,
                metrics.win_rate,
                metrics.expectancy,
                metrics.profit_factor,
                metrics.max_drawdown_pct,
            ),
        )
        await self.db.conn.commit()
        return metrics

    async def check_and_rollback_if_degraded(
        self,
        recent_trades: list[dict[str, Any]],
        min_eval_trades: int = 10,
        initial_capital_sol: float = 10.0,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """
        Evaluate performance of the currently active config version on `recent_trades`.
        If at least `min_eval_trades` have completed and performance is worse than the
        previous version (or expectancy is negative while previous was positive),
        automatically rolls back to the previous version.

        Returns
        -------
        (rolled_back, restored_params_snapshot, reason)
        """
        if len(recent_trades) < min_eval_trades:
            return False, None, f"Waiting for min evaluation trades ({len(recent_trades)}/{min_eval_trades})"

        active = await self.db.get_active_version_with_previous() if hasattr(self.db, "get_active_version_with_previous") else None
        cursor = await self.db.conn.execute(
            "SELECT * FROM config_versions ORDER BY id DESC LIMIT 2"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        if len(rows) < 2:
            return False, None, "No previous version available to roll back to"

        current_ver = rows[0]
        prev_ver = rows[1]

        current_metrics = await self.update_active_version_metrics(
            recent_trades, initial_capital_sol=initial_capital_sol
        )
        prev_expectancy = float(prev_ver.get("expectancy") or 0.0)
        prev_dd = float(prev_ver.get("max_drawdown_pct") or 0.0)

        # Degradation condition:
        # 1. Expectancy dropped significantly below previous version's expectancy, OR
        # 2. Expectancy turned negative AND max drawdown increased
        degraded = (
            current_metrics.expectancy < (prev_expectancy - 0.005)
            or (current_metrics.expectancy < 0 and current_metrics.max_drawdown_pct > prev_dd + 2.0)
        )

        if not degraded:
            return False, None, "Active config version performing within acceptable bounds"

        now = utc_now_iso()
        await self.db.conn.execute(
            "UPDATE config_versions SET is_active = FALSE, deactivated_at = ? WHERE id = ?",
            (now, current_ver["id"]),
        )
        await self.db.conn.execute(
            "UPDATE config_versions SET is_active = TRUE, activated_at = ?, deactivated_at = NULL WHERE id = ?",
            (now, prev_ver["id"]),
        )

        if current_ver.get("proposal_id"):
            await self.db.conn.execute(
                "UPDATE optimization_proposals SET status = ?, decided_at = ? WHERE id = ?",
                (ProposalStatus.ROLLED_BACK.value, now, current_ver["proposal_id"]),
            )

        await self.db.conn.commit()

        restored_snapshot = json.loads(prev_ver["config_snapshot"])
        reason = (
            f"Auto-rollback triggered: {current_ver['version_tag']} expectancy "
            f"({current_metrics.expectancy:+.4f} SOL) degraded vs {prev_ver['version_tag']} "
            f"({prev_expectancy:+.4f} SOL). Restored {prev_ver['version_tag']}."
        )
        logger.warning(reason)
        return True, restored_snapshot, reason

