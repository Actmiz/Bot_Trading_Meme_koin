try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


class BotMode(StrEnum):
    """Operating mode for the bot."""

    PAPER = "PAPER"
    DEVNET = "DEVNET"
    LIVE = "LIVE"


class TradeStatus(StrEnum):
    """Status of a trade."""

    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class DecisionType(StrEnum):
    """Type of decision logged."""

    SKIP_SCREENING = "skip_screening"
    SKIP_SCORE = "skip_score"
    ENTRY = "entry"
    EXIT = "exit"
    ERROR = "error"
    RISK_TRIGGERED = "risk_triggered"


class ExitReason(StrEnum):
    """Reason for exiting a position."""

    TP1 = "tp1"
    TP2 = "tp2"
    TP3 = "tp3"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    EMERGENCY_LIQUIDITY = "emergency_liquidity"
    EMERGENCY_RUG = "emergency_rug"
    KILL_SWITCH = "kill_switch"
    DAILY_LIMIT = "daily_limit"
    MAX_DRAWDOWN = "max_drawdown"
    MANUAL = "manual"


class RiskEventType(StrEnum):
    """Type of risk event."""

    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_DRAWDOWN = "max_drawdown"
    CIRCUIT_BREAKER = "circuit_breaker"
    KILL_SWITCH = "kill_switch"
    MAX_POSITIONS = "max_positions"


class ScreeningGate(StrEnum):
    """Individual screening gate identifiers."""

    MINT_AUTHORITY = "mint_authority_revoked"
    FREEZE_AUTHORITY = "freeze_authority_revoked"
    HOLDER_CONCENTRATION = "holder_concentration_ok"
    LIQUIDITY = "liquidity_sufficient"
    LP_STATUS = "lp_status"
    HONEYPOT = "honeypot_safe"
    RUGCHECK = "rugcheck_score"
    DEPLOYER = "deployer_history_ok"


class TokenSource(StrEnum):
    """Source where a token was detected."""

    DEXSCREENER = "dexscreener"
    BIRDEYE = "birdeye"
    HELIUS_WS = "helius_ws"


class DexType(StrEnum):
    """Supported DEXes."""

    RAYDIUM = "raydium"
    PUMPFUN = "pumpfun"
    ORCA = "orca"


class LPStatus(StrEnum):
    """Liquidity pool lock status."""

    BURNED = "burned"
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    UNKNOWN = "unknown"


class ProposalStatus(StrEnum):
    """Status of an optimization proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPLIED = "auto_applied"
    ROLLED_BACK = "rolled_back"
