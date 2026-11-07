"""Custom exceptions for the trading bot."""


class BotError(Exception):
    """Base exception for all bot errors."""


# --- Config ---
class ConfigError(BotError):
    """Raised when configuration is invalid or missing."""


class MissingEnvError(ConfigError):
    """Raised when a required environment variable is missing."""

    def __init__(self, var_name: str) -> None:
        self.var_name = var_name
        super().__init__(f"Required environment variable not set: {var_name}")


# --- Mode ---
class LiveModeError(BotError):
    """Raised when trying to activate LIVE mode without proper confirmation."""


# --- Screening ---
class ScreeningError(BotError):
    """Raised when a screening check encounters an unexpected error."""


# --- Execution ---
class ExecutionError(BotError):
    """Raised when a transaction execution fails."""


class SlippageExceededError(ExecutionError):
    """Raised when slippage exceeds the configured maximum."""


class InsufficientBalanceError(ExecutionError):
    """Raised when wallet balance is too low for the trade."""


# --- Risk ---
class RiskLimitError(BotError):
    """Raised when a risk limit is breached."""


class DailyLossLimitError(RiskLimitError):
    """Raised when daily loss limit is reached."""


class MaxDrawdownError(RiskLimitError):
    """Raised when maximum drawdown is reached."""


class CircuitBreakerError(RiskLimitError):
    """Raised when circuit breaker is triggered."""


class KillSwitchError(RiskLimitError):
    """Raised when kill switch is activated."""


# --- API ---
class APIError(BotError):
    """Raised when an external API call fails."""

    def __init__(self, service: str, message: str, status_code: int | None = None) -> None:
        self.service = service
        self.status_code = status_code
        super().__init__(f"[{service}] {message} (status={status_code})")


class RateLimitError(APIError):
    """Raised when an API rate limit is hit."""


# --- Database ---
class DatabaseError(BotError):
    """Raised when a database operation fails."""
