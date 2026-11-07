"""Kill Switch — emergency stop triggered via file flag (./KILL_SWITCH) or Telegram command."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class KillSwitch:
    """
    Monitors both a filesystem flag (`kill_switch_file`) and an in-memory/Telegram
    command flag. If either is active, the bot immediately halts and closes all positions.
    """

    def __init__(self, file_path: str | Path = "./KILL_SWITCH") -> None:
        self.file_path = Path(file_path)
        self._manual_active: bool = False
        self._reason: str | None = None
        self._source: str | None = None

    def is_active(self) -> tuple[bool, str | None]:
        """
        Check whether the kill switch is currently active.
        Returns (active, reason).
        """
        if self._manual_active:
            return True, self._reason or f"Kill switch activated via {self._source or 'command'}"

        if self.file_path.exists():
            try:
                content = self.file_path.read_text(encoding="utf-8").strip()
            except OSError:
                content = ""
            reason = f"Kill switch file detected ({self.file_path.name})"
            if content:
                reason = f"{reason}: {content}"
            return True, reason

        return False, None

    def activate(self, source: str = "telegram", reason: str = "Manual kill command", create_file: bool = False) -> str:
        """
        Trigger the kill switch programmatically (e.g., from a Telegram `/kill` command).
        Optionally writes the kill switch flag file to disk for persistence across restarts.
        """
        self._manual_active = True
        self._source = source
        self._reason = f"[{source}] {reason}"

        if create_file:
            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                self.file_path.write_text(self._reason, encoding="utf-8")
            except OSError as e:
                logger.error("Failed to write kill switch file %s: %s", self.file_path, e)

        logger.critical("KILL SWITCH ACTIVATED: %s", self._reason)
        return self._reason

    def reset(self, remove_file: bool = True) -> None:
        """Reset the kill switch state (requires explicit human action)."""
        self._manual_active = False
        self._reason = None
        self._source = None
        if remove_file and self.file_path.exists():
            try:
                self.file_path.unlink()
            except OSError as e:
                logger.error("Failed to remove kill switch file %s: %s", self.file_path, e)
        logger.info("Kill switch reset.")

