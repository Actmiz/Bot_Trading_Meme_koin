"""SQLite database manager — async connection, schema migration, CRUD helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.config import BotConfig, DatabaseConfig
from src.core.exceptions import DatabaseError as BotDatabaseError
from src.core.models import (
    TokenSnapshot,
    ScreeningResult,
    ScoringResult,
    TradeRecord,
    RiskEvent,
    DecisionLog,
    ConfigVersion,
    OptimizationProposal,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema SQL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
-- =============================================
-- Tokens
-- =============================================
CREATE TABLE IF NOT EXISTS tokens (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    mint_address          TEXT    NOT NULL UNIQUE,
    symbol                TEXT,
    name                  TEXT,
    source                TEXT    NOT NULL,
    pair_address          TEXT,
    dex                   TEXT,
    deployer_address      TEXT,
    detected_at           TEXT    NOT NULL,
    token_age_seconds     INTEGER,
    initial_price_usd     REAL,
    initial_mcap_usd      REAL,
    initial_liquidity_usd REAL,
    initial_volume_24h    REAL,
    initial_holders       INTEGER,
    initial_buy_count     INTEGER,
    initial_sell_count    INTEGER,
    metadata_json         TEXT,
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tokens_detected_at ON tokens(detected_at);
CREATE INDEX IF NOT EXISTS idx_tokens_mint ON tokens(mint_address);

-- =============================================
-- Screening Results
-- =============================================
CREATE TABLE IF NOT EXISTS screening_results (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id                 INTEGER NOT NULL REFERENCES tokens(id),
    mint_address             TEXT    NOT NULL,
    mint_authority_revoked   BOOLEAN,
    freeze_authority_revoked BOOLEAN,
    holder_concentration_ok  BOOLEAN,
    top10_holder_pct         REAL,
    liquidity_sufficient     BOOLEAN,
    liquidity_usd            REAL,
    lp_status                TEXT,
    honeypot_safe            BOOLEAN,
    sell_tax_pct             REAL,
    transfer_fee_pct         REAL,
    rugcheck_score           REAL,
    rugcheck_risks           TEXT,
    deployer_history_ok      BOOLEAN,
    deployer_risk_notes      TEXT,
    passed                   BOOLEAN NOT NULL,
    fail_reasons             TEXT,
    screening_duration_ms    INTEGER,
    screened_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_screening_token ON screening_results(token_id);
CREATE INDEX IF NOT EXISTS idx_screening_passed ON screening_results(passed);

-- =============================================
-- Scoring Results
-- =============================================
CREATE TABLE IF NOT EXISTS scoring_results (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id                    INTEGER NOT NULL REFERENCES tokens(id),
    screening_id                INTEGER NOT NULL REFERENCES screening_results(id),
    feat_price_momentum         REAL,
    feat_volume_liquidity_ratio REAL,
    feat_holder_growth_rate     REAL,
    feat_buy_sell_ratio         REAL,
    feat_token_age_score        REAL,
    total_score                 REAL    NOT NULL,
    threshold_used              REAL    NOT NULL,
    above_threshold             BOOLEAN NOT NULL,
    weights_version             TEXT,
    weights_json                TEXT,
    scored_at                   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scoring_token ON scoring_results(token_id);
CREATE INDEX IF NOT EXISTS idx_scoring_score ON scoring_results(total_score);

-- =============================================
-- Trades
-- =============================================
CREATE TABLE IF NOT EXISTS trades (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id             INTEGER NOT NULL REFERENCES tokens(id),
    scoring_id           INTEGER REFERENCES scoring_results(id),
    mint_address         TEXT    NOT NULL,
    mode                 TEXT    NOT NULL,
    entry_price_usd      REAL,
    entry_amount_sol     REAL,
    entry_amount_tokens  REAL,
    entry_slippage_pct   REAL,
    entry_fee_sol        REAL,
    entry_priority_fee   REAL,
    entry_tx_signature   TEXT,
    entry_method         TEXT,
    entry_at             TEXT,
    exit_price_usd       REAL,
    exit_amount_sol      REAL,
    exit_slippage_pct    REAL,
    exit_fee_sol         REAL,
    exit_tx_signature    TEXT,
    exit_reason          TEXT,
    exit_at              TEXT,
    pnl_sol              REAL,
    pnl_usd              REAL,
    pnl_pct              REAL,
    holding_duration_sec INTEGER,
    status               TEXT    NOT NULL DEFAULT 'open',
    entry_features_json  TEXT,
    entry_score          REAL,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);
CREATE INDEX IF NOT EXISTS idx_trades_entry_at ON trades(entry_at);
CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint_address);

-- =============================================
-- Risk Events
-- =============================================
CREATE TABLE IF NOT EXISTS risk_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type           TEXT    NOT NULL,
    details              TEXT,
    action_taken         TEXT,
    daily_pnl_sol        REAL,
    total_drawdown_pct   REAL,
    consecutive_failures INTEGER,
    occurred_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_risk_events_type ON risk_events(event_type);

-- =============================================
-- Decisions Log
-- =============================================
CREATE TABLE IF NOT EXISTS decisions_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_type     TEXT    NOT NULL,
    mint_address      TEXT,
    token_id          INTEGER REFERENCES tokens(id),
    trade_id          INTEGER REFERENCES trades(id),
    reason            TEXT,
    details_json      TEXT,
    decided_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_type ON decisions_log(decision_type);
CREATE INDEX IF NOT EXISTS idx_decisions_mint ON decisions_log(mint_address);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions_log(decided_at);

-- =============================================
-- Config Versions
-- =============================================
CREATE TABLE IF NOT EXISTS config_versions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    version_tag        TEXT    NOT NULL UNIQUE,
    config_snapshot    TEXT    NOT NULL,
    change_description TEXT,
    change_source      TEXT,
    proposal_id        INTEGER,
    trades_count       INTEGER DEFAULT 0,
    win_rate           REAL,
    expectancy         REAL,
    profit_factor      REAL,
    max_drawdown_pct   REAL,
    is_active          BOOLEAN NOT NULL DEFAULT FALSE,
    activated_at       TEXT,
    deactivated_at     TEXT,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- =============================================
-- Optimization Proposals
-- =============================================
CREATE TABLE IF NOT EXISTS optimization_proposals (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_period          TEXT,
    trades_analyzed          INTEGER,
    patterns_found           TEXT,
    proposed_changes         TEXT    NOT NULL,
    rationale                TEXT,
    backtest_result          TEXT,
    backtest_win_rate        REAL,
    backtest_expectancy      REAL,
    backtest_improvement_pct REAL,
    sample_size              INTEGER,
    passed_validation        BOOLEAN,
    status                   TEXT    NOT NULL DEFAULT 'pending',
    decided_by               TEXT,
    applied_version          TEXT,
    created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    decided_at               TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON optimization_proposals(status);
"""


# ---------------------------------------------------------------------------
# Helper: serialize dicts/lists to JSON strings for TEXT columns
# ---------------------------------------------------------------------------
def _to_json(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------
class Database:
    """Async SQLite database manager."""

    def __init__(self, config: DatabaseConfig | None = None, db_path: str | None = None) -> None:
        """
        Parameters
        ----------
        config  : DatabaseConfig from the loaded BotConfig.
        db_path : Override path (useful for tests with :memory:).
        """
        if db_path:
            self._path = db_path
        elif config:
            self._path = config.path
        else:
            self._path = "./data/bot.db"

        self._wal = config.wal_mode if config else True
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Open database connection and ensure schema exists."""
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row

        if self._wal:
            await self._conn.execute("PRAGMA journal_mode=WAL")

        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA_SQL)
        await self._conn.commit()
        logger.info("Database connected: %s", self._path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise BotDatabaseError("Database not connected. Call connect() first.")
        return self._conn

    # ------------------------------------------------------------------
    # Token CRUD
    # ------------------------------------------------------------------
    async def insert_token(self, token: TokenSnapshot) -> int:
        """Insert a new token snapshot. Returns the row id."""
        sql = """
            INSERT OR IGNORE INTO tokens (
                mint_address, symbol, name, source, pair_address, dex,
                deployer_address, detected_at, token_age_seconds,
                initial_price_usd, initial_mcap_usd, initial_liquidity_usd,
                initial_volume_24h, initial_holders, initial_buy_count,
                initial_sell_count, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            token.mint_address,
            token.symbol,
            token.name,
            token.source.value,
            token.pair_address,
            token.dex.value if token.dex else None,
            token.deployer_address,
            token.detected_at,
            token.token_age_seconds,
            token.initial_price_usd,
            token.initial_mcap_usd,
            token.initial_liquidity_usd,
            token.initial_volume_24h,
            token.initial_holders,
            token.initial_buy_count,
            token.initial_sell_count,
            _to_json(token.metadata_json),
        ))
        await self.conn.commit()

        # If INSERT OR IGNORE skipped (duplicate), fetch existing id
        if cursor.lastrowid == 0 or cursor.rowcount == 0:
            row = await self.conn.execute_fetchall(
                "SELECT id FROM tokens WHERE mint_address = ?", (token.mint_address,)
            )
            return row[0][0] if row else 0

        logger.debug("Inserted token id=%d mint=%s", cursor.lastrowid, token.mint_address)
        return cursor.lastrowid

    async def get_token_by_mint(self, mint_address: str) -> dict[str, Any] | None:
        """Fetch a token by mint address."""
        cursor = await self.conn.execute(
            "SELECT * FROM tokens WHERE mint_address = ?", (mint_address,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Screening CRUD
    # ------------------------------------------------------------------
    async def insert_screening(self, result: ScreeningResult) -> int:
        """Insert a screening result. Returns row id."""
        sql = """
            INSERT INTO screening_results (
                token_id, mint_address, mint_authority_revoked,
                freeze_authority_revoked, holder_concentration_ok,
                top10_holder_pct, liquidity_sufficient, liquidity_usd,
                lp_status, honeypot_safe, sell_tax_pct, transfer_fee_pct,
                rugcheck_score, rugcheck_risks, deployer_history_ok,
                deployer_risk_notes, passed, fail_reasons,
                screening_duration_ms, screened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            result.token_id,
            result.mint_address,
            result.mint_authority_revoked,
            result.freeze_authority_revoked,
            result.holder_concentration_ok,
            result.top10_holder_pct,
            result.liquidity_sufficient,
            result.liquidity_usd,
            result.lp_status.value if result.lp_status else None,
            result.honeypot_safe,
            result.sell_tax_pct,
            result.transfer_fee_pct,
            result.rugcheck_score,
            _to_json(result.rugcheck_risks),
            result.deployer_history_ok,
            result.deployer_risk_notes,
            result.passed,
            _to_json(result.fail_reasons),
            result.screening_duration_ms,
            result.screened_at,
        ))
        await self.conn.commit()
        logger.debug("Inserted screening id=%d token_id=%d passed=%s",
                      cursor.lastrowid, result.token_id, result.passed)
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # Scoring CRUD
    # ------------------------------------------------------------------
    async def insert_scoring(self, result: ScoringResult) -> int:
        """Insert a scoring result. Returns row id."""
        sql = """
            INSERT INTO scoring_results (
                token_id, screening_id, feat_price_momentum,
                feat_volume_liquidity_ratio, feat_holder_growth_rate,
                feat_buy_sell_ratio, feat_token_age_score,
                total_score, threshold_used, above_threshold,
                weights_version, weights_json, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            result.token_id,
            result.screening_id,
            result.feat_price_momentum,
            result.feat_volume_liquidity_ratio,
            result.feat_holder_growth_rate,
            result.feat_buy_sell_ratio,
            result.feat_token_age_score,
            result.total_score,
            result.threshold_used,
            result.above_threshold,
            result.weights_version,
            _to_json(result.weights_json),
            result.scored_at,
        ))
        await self.conn.commit()
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # Trade CRUD
    # ------------------------------------------------------------------
    async def insert_trade(self, trade: TradeRecord) -> int:
        """Insert a new trade. Returns row id."""
        sql = """
            INSERT INTO trades (
                token_id, scoring_id, mint_address, mode,
                entry_price_usd, entry_amount_sol, entry_amount_tokens,
                entry_slippage_pct, entry_fee_sol, entry_priority_fee,
                entry_tx_signature, entry_method, entry_at,
                status, entry_features_json, entry_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            trade.token_id,
            trade.scoring_id,
            trade.mint_address,
            trade.mode,
            trade.entry_price_usd,
            trade.entry_amount_sol,
            trade.entry_amount_tokens,
            trade.entry_slippage_pct,
            trade.entry_fee_sol,
            trade.entry_priority_fee,
            trade.entry_tx_signature,
            trade.entry_method,
            trade.entry_at,
            trade.status.value,
            _to_json(trade.entry_features_json),
            trade.entry_score,
        ))
        await self.conn.commit()
        logger.debug("Inserted trade id=%d mint=%s", cursor.lastrowid, trade.mint_address)
        return cursor.lastrowid

    async def update_trade_exit(
        self,
        trade_id: int,
        exit_price_usd: float,
        exit_amount_sol: float,
        exit_slippage_pct: float,
        exit_fee_sol: float,
        exit_tx_signature: str | None,
        exit_reason: str,
        pnl_sol: float,
        pnl_usd: float,
        pnl_pct: float,
        holding_duration_sec: int,
    ) -> None:
        """Update a trade with exit data."""
        now = utc_now_iso()
        sql = """
            UPDATE trades SET
                exit_price_usd = ?, exit_amount_sol = ?, exit_slippage_pct = ?,
                exit_fee_sol = ?, exit_tx_signature = ?, exit_reason = ?,
                exit_at = ?, pnl_sol = ?, pnl_usd = ?, pnl_pct = ?,
                holding_duration_sec = ?, status = 'closed', updated_at = ?
            WHERE id = ?
        """
        await self.conn.execute(sql, (
            exit_price_usd, exit_amount_sol, exit_slippage_pct,
            exit_fee_sol, exit_tx_signature, exit_reason,
            now, pnl_sol, pnl_usd, pnl_pct,
            holding_duration_sec, now, trade_id,
        ))
        await self.conn.commit()
        logger.debug("Updated trade id=%d exit_reason=%s pnl_sol=%.6f",
                      trade_id, exit_reason, pnl_sol)

    async def get_open_trades(self) -> list[dict[str, Any]]:
        """Fetch all open trades."""
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY entry_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trades_since(self, since_iso: str) -> list[dict[str, Any]]:
        """Fetch trades with entry_at >= since_iso."""
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE entry_at >= ? ORDER BY entry_at", (since_iso,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_daily_pnl(self, date_iso: str) -> float:
        """Sum PnL (SOL) for trades closed on a given date (YYYY-MM-DD)."""
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(pnl_sol), 0) FROM trades "
            "WHERE status = 'closed' AND exit_at LIKE ?",
            (f"{date_iso}%",),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    # ------------------------------------------------------------------
    # Risk Events CRUD
    # ------------------------------------------------------------------
    async def insert_risk_event(self, event: RiskEvent) -> int:
        """Insert a risk event. Returns row id."""
        sql = """
            INSERT INTO risk_events (
                event_type, details, action_taken,
                daily_pnl_sol, total_drawdown_pct,
                consecutive_failures, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            event.event_type.value,
            _to_json(event.details),
            event.action_taken,
            event.daily_pnl_sol,
            event.total_drawdown_pct,
            event.consecutive_failures,
            event.occurred_at,
        ))
        await self.conn.commit()
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # Decision Log CRUD
    # ------------------------------------------------------------------
    async def insert_decision(self, decision: DecisionLog) -> int:
        """Insert a decision log entry. Returns row id."""
        sql = """
            INSERT INTO decisions_log (
                decision_type, mint_address, token_id, trade_id,
                reason, details_json, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            decision.decision_type.value,
            decision.mint_address,
            decision.token_id,
            decision.trade_id,
            decision.reason,
            _to_json(decision.details_json),
            decision.decided_at,
        ))
        await self.conn.commit()
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # Config Version CRUD
    # ------------------------------------------------------------------
    async def insert_config_version(self, cv: ConfigVersion) -> int:
        """Insert a config version snapshot."""
        sql = """
            INSERT INTO config_versions (
                version_tag, config_snapshot, change_description,
                change_source, proposal_id, is_active, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            cv.version_tag,
            _to_json(cv.config_snapshot),
            cv.change_description,
            cv.change_source,
            cv.proposal_id,
            cv.is_active,
            cv.activated_at,
        ))
        await self.conn.commit()
        return cursor.lastrowid

    async def get_active_config_version(self) -> dict[str, Any] | None:
        """Fetch the currently active config version."""
        cursor = await self.conn.execute(
            "SELECT * FROM config_versions WHERE is_active = TRUE LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Optimization Proposals CRUD
    # ------------------------------------------------------------------
    async def insert_proposal(self, proposal: OptimizationProposal) -> int:
        """Insert an optimization proposal."""
        sql = """
            INSERT INTO optimization_proposals (
                analysis_period, trades_analyzed, patterns_found,
                proposed_changes, rationale, backtest_result,
                backtest_win_rate, backtest_expectancy,
                backtest_improvement_pct, sample_size,
                passed_validation, status, decided_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self.conn.execute(sql, (
            proposal.analysis_period,
            proposal.trades_analyzed,
            _to_json(proposal.patterns_found),
            _to_json(proposal.proposed_changes),
            proposal.rationale,
            _to_json(proposal.backtest_result),
            proposal.backtest_win_rate,
            proposal.backtest_expectancy,
            proposal.backtest_improvement_pct,
            proposal.sample_size,
            proposal.passed_validation,
            proposal.status.value,
            proposal.decided_by,
        ))
        await self.conn.commit()
        return cursor.lastrowid

    async def get_pending_proposals(self) -> list[dict[str, Any]]:
        """Fetch all pending optimization proposals."""
        cursor = await self.conn.execute(
            "SELECT * FROM optimization_proposals WHERE status = 'pending' ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------
    async def count_open_positions(self) -> int:
        """Count currently open trades."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_all_closed_trades(self) -> list[dict[str, Any]]:
        """Fetch all closed trades (for reporting)."""
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_at"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
