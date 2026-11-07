"""Configuration loader — merges config.yaml + .env with validation."""

from __future__ import annotations

import os
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import yaml
from dotenv import load_dotenv

from src.core.enums import BotMode
from src.core.exceptions import ConfigError, MissingEnvError, LiveModeError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# Pydantic-free dataclasses (minimal deps, easy to test)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TakeProfitLevel:
    pct: float
    sell_pct: float


@dataclass(frozen=True)
class TrailingStopConfig:
    activation_pct: float = 30.0
    trail_pct: float = 20.0


@dataclass(frozen=True)
class EmergencyConfig:
    liquidity_drop_pct: float = 50.0
    rug_indicators: list[str] = field(default_factory=lambda: [
        "large_sell_deployer", "lp_removed", "mint_enabled"
    ])


@dataclass(frozen=True)
class DetectionConfig:
    poll_interval_sec: int = 10
    min_liquidity_usd: float = 5000
    max_token_age_sec: int = 3600
    min_volume_usd: float = 1000
    dexes: list[str] = field(default_factory=lambda: ["raydium", "pumpfun"])
    dexscreener_enabled: bool = True
    dexscreener_base_url: str = "https://api.dexscreener.com/latest"
    birdeye_enabled: bool = True
    birdeye_base_url: str = "https://public-api.birdeye.so"
    helius_ws_enabled: bool = True


@dataclass(frozen=True)
class ScreeningConfig:
    top10_holder_max_pct: float = 50.0
    min_liquidity_usd: float = 10000
    lp_must_be_burned_or_locked: bool = True
    max_sell_tax_pct: float = 5.0
    max_transfer_fee_pct: float = 3.0
    rugcheck_enabled: bool = True
    rugcheck_min_score: float = 50
    deployer_check_enabled: bool = True
    deployer_max_rugged: int = 1
    # Pump.fun-specific gate (bonding-curve phase, dev bundles, insider dumps)
    pumpfun_enabled: bool = True
    pumpfun_top10_max_pct: float = 35.0
    pumpfun_min_curve_mcap_usd: float = 35000.0
    pumpfun_min_age_sec: int = 120
    pumpfun_block_bundle_risks: bool = True
    pumpfun_max_m5_dump_pct: float = -25.0


@dataclass(frozen=True)
class ScoringConfig:
    threshold: float = 0.65
    weights: dict[str, float] = field(default_factory=lambda: {
        "price_momentum": 0.25,
        "volume_liquidity_ratio": 0.20,
        "holder_growth": 0.20,
        "buy_sell_ratio": 0.20,
        "token_age": 0.15,
    })
    weight_bounds: dict[str, list[float]] = field(default_factory=lambda: {
        "price_momentum": [0.10, 0.40],
        "volume_liquidity_ratio": [0.10, 0.35],
        "holder_growth": [0.10, 0.35],
        "buy_sell_ratio": [0.10, 0.35],
        "token_age": [0.05, 0.25],
    })


@dataclass(frozen=True)
class ExecutionConfig:
    max_slippage_bps: int = 500
    priority_fee_strategy: str = "dynamic"
    priority_fee_fixed_lamports: int = 10000
    priority_fee_dynamic_percentile: int = 75
    max_retries: int = 3
    retry_delay_ms: int = 1000
    use_jito_bundle: bool = True
    jito_tip_lamports: int = 10000
    paper_sol_price_usd: float = 150.0  # Reference SOL/USD for paper simulation


@dataclass(frozen=True)
class PositionConfig:
    max_position_pct: float = 2.0
    max_position_sol: float = 0.5
    max_open_positions: int = 5
    take_profit_levels: list[TakeProfitLevel] = field(default_factory=lambda: [
        TakeProfitLevel(pct=50, sell_pct=30),
        TakeProfitLevel(pct=100, sell_pct=30),
        TakeProfitLevel(pct=200, sell_pct=100),
    ])
    stop_loss_pct: float = -30.0
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    time_stop_minutes: int = 60
    emergency: EmergencyConfig = field(default_factory=EmergencyConfig)


@dataclass(frozen=True)
class CircuitBreakerConfig:
    consecutive_failures: int = 3
    pause_duration_sec: int = 300


@dataclass(frozen=True)
class RiskConfig:
    daily_loss_limit_sol: float = 2.0
    max_drawdown_pct: float = 15.0
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    kill_switch_file: str = "./KILL_SWITCH"


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    notify_on: list[str] = field(default_factory=lambda: [
        "entry", "exit", "error", "risk_limit", "daily_report"
    ])


@dataclass(frozen=True)
class LearningConfig:
    analysis_interval_hours: int = 24
    min_trades_for_proposal: int = 20
    max_param_change_pct: float = 10.0
    auto_apply_in_paper: bool = False
    require_manual_approval: bool = True
    walk_forward_splits: int = 5
    out_of_sample_pct: float = 30
    min_sample_trades: int = 30


@dataclass(frozen=True)
class DatabaseConfig:
    path: str = "./data/bot.db"
    wal_mode: bool = True


@dataclass(frozen=True)
class EnvSecrets:
    """Secrets loaded exclusively from environment variables."""

    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_devnet_rpc_url: str = "https://api.devnet.solana.com"
    helius_api_key: str = ""
    helius_ws_url: str = ""
    birdeye_api_key: str = ""
    rugcheck_api_url: str = "https://api.rugcheck.xyz/v1"
    jupiter_api_url: str = "https://quote-api.jup.ag/v6"
    jito_block_engine_url: str = "https://mainnet.block-engine.jito.wtf"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    wallet_private_key: str = ""  # NEVER log this


@dataclass(frozen=True)
class BotConfig:
    """Top-level configuration container."""

    mode: BotMode = BotMode.PAPER
    live_confirm_required: bool = True
    log_level: str = "INFO"

    detection: DetectionConfig = field(default_factory=DetectionConfig)
    screening: ScreeningConfig = field(default_factory=ScreeningConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    secrets: EnvSecrets = field(default_factory=EnvSecrets)


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML config file."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must be a YAML mapping: {path}")
    return data


def _load_env_secrets() -> EnvSecrets:
    """Load secrets from environment variables only."""
    return EnvSecrets(
        solana_rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        solana_devnet_rpc_url=os.getenv("SOLANA_DEVNET_RPC_URL", "https://api.devnet.solana.com"),
        helius_api_key=os.getenv("HELIUS_API_KEY", ""),
        helius_ws_url=os.getenv("HELIUS_WS_URL", ""),
        birdeye_api_key=os.getenv("BIRDEYE_API_KEY", ""),
        rugcheck_api_url=os.getenv("RUGCHECK_API_URL", "https://api.rugcheck.xyz/v1"),
        jupiter_api_url=os.getenv("JUPITER_API_URL", "https://quote-api.jup.ag/v6"),
        jito_block_engine_url=os.getenv(
            "JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf"
        ),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        wallet_private_key=os.getenv("WALLET_PRIVATE_KEY", ""),
    )


def _parse_detection(raw: dict[str, Any]) -> DetectionConfig:
    """Parse detection section from raw YAML."""
    sources = raw.get("sources", {})
    filters_ = raw.get("filters", {})
    return DetectionConfig(
        poll_interval_sec=raw.get("poll_interval_sec", 10),
        min_liquidity_usd=filters_.get("min_liquidity_usd", 5000),
        max_token_age_sec=filters_.get("max_token_age_sec", 3600),
        min_volume_usd=filters_.get("min_volume_usd", 1000),
        dexes=filters_.get("dexes", ["raydium", "pumpfun"]),
        dexscreener_enabled=sources.get("dexscreener", {}).get("enabled", True),
        dexscreener_base_url=sources.get("dexscreener", {}).get(
            "base_url", "https://api.dexscreener.com/latest"
        ),
        birdeye_enabled=sources.get("birdeye", {}).get("enabled", True),
        birdeye_base_url=sources.get("birdeye", {}).get(
            "base_url", "https://public-api.birdeye.so"
        ),
        helius_ws_enabled=sources.get("helius_ws", {}).get("enabled", True),
    )


def _parse_screening(raw: dict[str, Any]) -> ScreeningConfig:
    """Parse screening section from raw YAML."""
    rc = raw.get("rugcheck", {})
    dc = raw.get("deployer_check", {})
    pf = raw.get("pumpfun", {}) or {}
    return ScreeningConfig(
        pumpfun_enabled=pf.get("enabled", True),
        pumpfun_top10_max_pct=float(pf.get("top10_holder_max_pct", 35.0)),
        pumpfun_min_curve_mcap_usd=float(pf.get("min_curve_mcap_usd", 35000.0)),
        pumpfun_min_age_sec=int(pf.get("min_age_sec", 120)),
        pumpfun_block_bundle_risks=pf.get("block_bundle_risks", True),
        pumpfun_max_m5_dump_pct=float(pf.get("max_m5_dump_pct", -25.0)),
        top10_holder_max_pct=raw.get("top10_holder_max_pct", 50.0),
        min_liquidity_usd=raw.get("min_liquidity_usd", 10000),
        lp_must_be_burned_or_locked=raw.get("lp_must_be_burned_or_locked", True),
        max_sell_tax_pct=raw.get("max_sell_tax_pct", 5.0),
        max_transfer_fee_pct=raw.get("max_transfer_fee_pct", 3.0),
        rugcheck_enabled=rc.get("enabled", True),
        rugcheck_min_score=rc.get("min_score", 50),
        deployer_check_enabled=dc.get("enabled", True),
        deployer_max_rugged=dc.get("max_rugged_deploys", 1),
    )


def _parse_scoring(raw: dict[str, Any]) -> ScoringConfig:
    """Parse scoring section from raw YAML."""
    default_cfg = ScoringConfig()
    return ScoringConfig(
        threshold=raw.get("threshold", default_cfg.threshold),
        weights=raw.get("weights", default_cfg.weights),
        weight_bounds=raw.get("weight_bounds", default_cfg.weight_bounds),
    )


def _parse_execution(raw: dict[str, Any]) -> ExecutionConfig:
    """Parse execution section from raw YAML."""
    pf = raw.get("priority_fee", {})
    return ExecutionConfig(
        max_slippage_bps=raw.get("max_slippage_bps", 500),
        priority_fee_strategy=pf.get("strategy", "dynamic"),
        priority_fee_fixed_lamports=pf.get("fixed_lamports", 10000),
        priority_fee_dynamic_percentile=pf.get("dynamic_percentile", 75),
        max_retries=raw.get("max_retries", 3),
        retry_delay_ms=raw.get("retry_delay_ms", 1000),
        use_jito_bundle=raw.get("use_jito_bundle", True),
        jito_tip_lamports=raw.get("jito_tip_lamports", 10000),
        paper_sol_price_usd=raw.get("paper_sol_price_usd", 150.0),
    )


def _parse_position(raw: dict[str, Any]) -> PositionConfig:
    """Parse position section from raw YAML."""
    tp_raw = raw.get("take_profit", [])
    tp_levels = [TakeProfitLevel(pct=tp["pct"], sell_pct=tp["sell_pct"]) for tp in tp_raw]

    ts_raw = raw.get("trailing_stop", {})
    trailing = TrailingStopConfig(
        activation_pct=ts_raw.get("activation_pct", 30.0),
        trail_pct=ts_raw.get("trail_pct", 20.0),
    )

    em_raw = raw.get("emergency", {})
    emergency = EmergencyConfig(
        liquidity_drop_pct=em_raw.get("liquidity_drop_pct", 50.0),
        rug_indicators=em_raw.get("rug_indicators", [
            "large_sell_deployer", "lp_removed", "mint_enabled"
        ]),
    )

    return PositionConfig(
        max_position_pct=raw.get("max_position_pct", 2.0),
        max_position_sol=raw.get("max_position_sol", 0.5),
        max_open_positions=raw.get("max_open_positions", 5),
        take_profit_levels=tp_levels if tp_levels else PositionConfig().take_profit_levels,
        stop_loss_pct=raw.get("stop_loss_pct", -30.0),
        trailing_stop=trailing,
        time_stop_minutes=raw.get("time_stop_minutes", 60),
        emergency=emergency,
    )


def _parse_risk(raw: dict[str, Any]) -> RiskConfig:
    """Parse risk section from raw YAML."""
    cb_raw = raw.get("circuit_breaker", {})
    return RiskConfig(
        daily_loss_limit_sol=raw.get("daily_loss_limit_sol", 2.0),
        max_drawdown_pct=raw.get("max_drawdown_pct", 15.0),
        circuit_breaker=CircuitBreakerConfig(
            consecutive_failures=cb_raw.get("consecutive_failures", 3),
            pause_duration_sec=cb_raw.get("pause_duration_sec", 300),
        ),
        kill_switch_file=raw.get("kill_switch_file", "./KILL_SWITCH"),
    )


def _parse_learning(raw: dict[str, Any]) -> LearningConfig:
    """Parse learning section from raw YAML."""
    bt = raw.get("backtest", {})
    return LearningConfig(
        analysis_interval_hours=raw.get("analysis_interval_hours", 24),
        min_trades_for_proposal=raw.get("min_trades_for_proposal", 20),
        max_param_change_pct=raw.get("max_param_change_pct", 10.0),
        auto_apply_in_paper=raw.get("auto_apply_in_paper", False),
        require_manual_approval=raw.get("require_manual_approval", True),
        walk_forward_splits=bt.get("walk_forward_splits", 5),
        out_of_sample_pct=bt.get("out_of_sample_pct", 30),
        min_sample_trades=bt.get("min_sample_trades", 30),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_config(config: BotConfig) -> None:
    """Validate the final config. Raises ConfigError on problems."""
    # Scoring weights must sum to ~1.0
    total_weight = sum(config.scoring.weights.values())
    if not (0.99 <= total_weight <= 1.01):
        raise ConfigError(
            f"Scoring weights must sum to 1.0, got {total_weight:.4f}"
        )

    # Each weight must be within bounds
    for key, weight in config.scoring.weights.items():
        bounds = config.scoring.weight_bounds.get(key)
        if bounds and not (bounds[0] <= weight <= bounds[1]):
            raise ConfigError(
                f"Scoring weight '{key}' = {weight} is outside bounds {bounds}"
            )

    # LIVE mode safety
    if config.mode == BotMode.LIVE:
        if config.live_confirm_required:
            raise LiveModeError(
                "LIVE mode requires explicit confirmation. "
                "Set live_confirm_required=false in config after careful review."
            )
        if not config.secrets.wallet_private_key:
            raise MissingEnvError("WALLET_PRIVATE_KEY")

    # Risk limits must be positive
    if config.risk.daily_loss_limit_sol <= 0:
        raise ConfigError("daily_loss_limit_sol must be positive")
    if config.risk.max_drawdown_pct <= 0:
        raise ConfigError("max_drawdown_pct must be positive")

    # Take-profit sell_pct should not exceed 100
    for tp in config.position.take_profit_levels:
        if tp.sell_pct > 100 or tp.sell_pct <= 0:
            raise ConfigError(f"Take-profit sell_pct must be in (0, 100], got {tp.sell_pct}")

    logger.info("Configuration validated successfully (mode=%s)", config.mode)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_config(
    config_path: Path | str | None = None,
    env_path: Path | str | None = None,
    *,
    validate: bool = True,
) -> BotConfig:
    """
    Load and return the full bot configuration.

    1. Load .env file (secrets).
    2. Parse config.yaml (non-secret parameters).
    3. Merge and validate.
    4. BOT_MODE env var overrides config.yaml.

    Parameters
    ----------
    config_path : path to config.yaml, defaults to project root.
    env_path    : path to .env, defaults to project root.
    validate    : whether to run validation checks.

    Returns
    -------
    BotConfig with all settings loaded.
    """
    config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    env_path = Path(env_path) if env_path else _DEFAULT_ENV_PATH

    # Load .env if it exists (does NOT override existing env vars)
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.debug("Loaded .env from %s", env_path)

    # Parse YAML
    raw = _load_yaml(config_path)

    # Determine mode: env var takes priority over yaml
    env_mode = os.getenv("BOT_MODE")
    yaml_mode = raw.get("bot", {}).get("mode", "PAPER")
    mode_str = env_mode if env_mode else yaml_mode
    try:
        mode = BotMode(mode_str.upper())
    except ValueError:
        raise ConfigError(f"Invalid BOT_MODE: '{mode_str}'. Must be PAPER, DEVNET, or LIVE.")

    bot_section = raw.get("bot", {})

    config = BotConfig(
        mode=mode,
        live_confirm_required=bot_section.get("live_confirm_required", True),
        log_level=bot_section.get("log_level", "INFO"),
        detection=_parse_detection(raw.get("detection", {})),
        screening=_parse_screening(raw.get("screening", {})),
        scoring=_parse_scoring(raw.get("scoring", {})),
        execution=_parse_execution(raw.get("execution", {})),
        position=_parse_position(raw.get("position", {})),
        risk=_parse_risk(raw.get("risk", {})),
        telegram=TelegramConfig(
            enabled=raw.get("telegram", {}).get("enabled", False),
            notify_on=raw.get("telegram", {}).get("notify_on", []),
        ),
        learning=_parse_learning(raw.get("learning", {})),
        database=DatabaseConfig(
            path=raw.get("database", {}).get("path", "./data/bot.db"),
            wal_mode=raw.get("database", {}).get("wal_mode", True),
        ),
        secrets=_load_env_secrets(),
    )

    if validate:
        _validate_config(config)

    logger.info("Config loaded — mode=%s, db=%s", config.mode, config.database.path)
    return config
