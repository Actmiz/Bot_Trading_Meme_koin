"""Hard-coded risk limits that CANNOT be overridden by config.yaml or the learning module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.core.config import RiskConfig


@dataclass(frozen=True)
class HardRiskCeilings:
    """Absolute safety ceilings. No config or optimizer can relax these bounds."""

    max_daily_loss_sol: float = 5.0
    max_drawdown_pct: float = 25.0
    max_consecutive_failures: int = 5
    min_pause_duration_sec: int = 60
    max_bot_wallet_sol: float = 20.0  # Dedicated bot wallet should only hold small capital
    min_bot_wallet_sol: float = 0.02


HARD_RISK_LIMITS: Final[HardRiskCeilings] = HardRiskCeilings()

# Config sections/prefixes that the self-learning optimizer is strictly forbidden to modify
PROTECTED_CONFIG_SECTIONS: Final[frozenset[str]] = frozenset({
    "risk",
    "screening",
    "bot",
    "secrets",
    "database",
})


@dataclass(frozen=True)
class EffectiveRiskLimits:
    """Effective risk limits after clamping config values against HARD_RISK_LIMITS."""

    daily_loss_limit_sol: float
    max_drawdown_pct: float
    consecutive_failures: int
    pause_duration_sec: int
    kill_switch_file: str


def enforce_hard_risk_ceilings(risk_cfg: RiskConfig) -> EffectiveRiskLimits:
    """
    Clamp user-configured risk parameters so they are NEVER looser than HARD_RISK_LIMITS.
    """
    daily_loss = min(
        max(0.01, risk_cfg.daily_loss_limit_sol),
        HARD_RISK_LIMITS.max_daily_loss_sol,
    )
    max_dd = min(
        max(1.0, risk_cfg.max_drawdown_pct),
        HARD_RISK_LIMITS.max_drawdown_pct,
    )
    max_fails = min(
        max(1, risk_cfg.circuit_breaker.consecutive_failures),
        HARD_RISK_LIMITS.max_consecutive_failures,
    )
    pause_sec = max(
        HARD_RISK_LIMITS.min_pause_duration_sec,
        risk_cfg.circuit_breaker.pause_duration_sec,
    )

    return EffectiveRiskLimits(
        daily_loss_limit_sol=daily_loss,
        max_drawdown_pct=max_dd,
        consecutive_failures=max_fails,
        pause_duration_sec=pause_sec,
        kill_switch_file=risk_cfg.kill_switch_file,
    )


def is_protected_parameter(param_path: str) -> bool:
    """
    Return True if `param_path` (e.g. 'risk.daily_loss_limit_sol' or 'screening.min_liquidity_usd')
    belongs to a protected section that the learning module must NEVER modify.
    """
    cleaned = param_path.strip().lower()
    prefix = cleaned.split(".")[0] if "." in cleaned else cleaned
    if prefix in PROTECTED_CONFIG_SECTIONS:
        return True
    # Also block any parameter name containing risk/security keywords
    forbidden_keywords = (
        "daily_loss",
        "drawdown",
        "circuit_breaker",
        "kill_switch",
        "authority",
        "honeypot",
        "rugcheck",
        "deployer",
        "live_confirm",
        "private_key",
    )
    return any(kw in cleaned for kw in forbidden_keywords)

