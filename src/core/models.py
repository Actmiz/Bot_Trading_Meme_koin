"""Data models (Pydantic) for structured data passed between modules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.core.enums import (
    TokenSource,
    DexType,
    LPStatus,
    TradeStatus,
    ExitReason,
    DecisionType,
    RiskEventType,
    ProposalStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return utc_now().isoformat()


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
class TokenSnapshot(BaseModel):
    """Snapshot of a newly detected token."""

    mint_address: str
    symbol: str | None = None
    name: str | None = None
    source: TokenSource
    pair_address: str | None = None
    dex: DexType | None = None
    deployer_address: str | None = None

    detected_at: str = Field(default_factory=utc_now_iso)
    token_age_seconds: int | None = None
    initial_price_usd: float | None = None
    initial_mcap_usd: float | None = None
    initial_liquidity_usd: float | None = None
    initial_volume_24h: float | None = None
    initial_holders: int | None = None
    initial_buy_count: int | None = None
    initial_sell_count: int | None = None

    metadata_json: dict[str, Any] | None = None

    @field_validator("mint_address")
    @classmethod
    def validate_mint(cls, v: str) -> str:
        if not v or len(v) < 32:
            raise ValueError(f"Invalid mint address: {v}")
        return v


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------
class ScreeningResult(BaseModel):
    """Result of all security screening gates for a token."""

    id: int | None = None  # Set after DB insert
    token_id: int
    mint_address: str

    # Individual gate results
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    holder_concentration_ok: bool | None = None
    top10_holder_pct: float | None = None
    liquidity_sufficient: bool | None = None
    liquidity_usd: float | None = None
    lp_status: LPStatus | None = None
    honeypot_safe: bool | None = None
    sell_tax_pct: float | None = None
    transfer_fee_pct: float | None = None
    rugcheck_score: float | None = None
    rugcheck_risks: list[str] | None = None
    deployer_history_ok: bool | None = None
    deployer_risk_notes: str | None = None

    # Final verdict
    passed: bool = False
    fail_reasons: list[str] = Field(default_factory=list)
    screening_duration_ms: int | None = None

    screened_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
class ScoringResult(BaseModel):
    """Scoring features and composite score for a token."""

    id: int | None = None  # Set after DB insert
    token_id: int
    screening_id: int

    feat_price_momentum: float | None = None
    feat_volume_liquidity_ratio: float | None = None
    feat_holder_growth_rate: float | None = None
    feat_buy_sell_ratio: float | None = None
    feat_token_age_score: float | None = None

    total_score: float
    threshold_used: float
    above_threshold: bool

    weights_version: str | None = None
    weights_json: dict[str, float] | None = None

    scored_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------
class TradeRecord(BaseModel):
    """Full trade record: entry + exit + PnL."""

    id: int | None = None
    token_id: int
    scoring_id: int | None = None
    mint_address: str
    mode: str  # BotMode value

    # Entry
    entry_price_usd: float | None = None
    entry_amount_sol: float | None = None
    entry_amount_tokens: float | None = None
    entry_slippage_pct: float | None = None
    entry_fee_sol: float | None = None
    entry_priority_fee: float | None = None
    entry_tx_signature: str | None = None
    entry_method: str | None = None
    entry_at: str | None = None

    # Exit
    exit_price_usd: float | None = None
    exit_amount_sol: float | None = None
    exit_slippage_pct: float | None = None
    exit_fee_sol: float | None = None
    exit_tx_signature: str | None = None
    exit_reason: ExitReason | None = None
    exit_at: str | None = None

    # PnL
    pnl_sol: float | None = None
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    holding_duration_sec: int | None = None

    # Status
    status: TradeStatus = TradeStatus.OPEN

    # Feature snapshot at entry
    entry_features_json: dict[str, Any] | None = None
    entry_score: float | None = None

    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Risk Event
# ---------------------------------------------------------------------------
class RiskEvent(BaseModel):
    """A risk control event (limit breach, circuit breaker, kill switch)."""

    event_type: RiskEventType
    details: dict[str, Any] | None = None
    action_taken: str | None = None
    daily_pnl_sol: float | None = None
    total_drawdown_pct: float | None = None
    consecutive_failures: int | None = None
    occurred_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Decision Log
# ---------------------------------------------------------------------------
class DecisionLog(BaseModel):
    """An auditable decision record."""

    decision_type: DecisionType
    mint_address: str | None = None
    token_id: int | None = None
    trade_id: int | None = None
    reason: str | None = None
    details_json: dict[str, Any] | None = None
    decided_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Config Version
# ---------------------------------------------------------------------------
class ConfigVersion(BaseModel):
    """A versioned snapshot of scoring/risk config."""

    version_tag: str
    config_snapshot: dict[str, Any]
    change_description: str | None = None
    change_source: str = "manual"
    proposal_id: int | None = None

    trades_count: int = 0
    win_rate: float | None = None
    expectancy: float | None = None
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None

    is_active: bool = False
    activated_at: str | None = None
    deactivated_at: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Optimization Proposal
# ---------------------------------------------------------------------------
class OptimizationProposal(BaseModel):
    """A proposed parameter change from the learning module."""

    analysis_period: str | None = None
    trades_analyzed: int | None = None
    patterns_found: list[str] | None = None

    proposed_changes: dict[str, Any]
    rationale: str | None = None

    backtest_result: dict[str, Any] | None = None
    backtest_win_rate: float | None = None
    backtest_expectancy: float | None = None
    backtest_improvement_pct: float | None = None
    sample_size: int | None = None
    passed_validation: bool | None = None

    status: ProposalStatus = ProposalStatus.PENDING
    decided_by: str | None = None
    applied_version: str | None = None

    created_at: str = Field(default_factory=utc_now_iso)
    decided_at: str | None = None
