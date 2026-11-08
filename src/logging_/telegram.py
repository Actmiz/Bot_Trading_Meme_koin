"""Telegram Bot API notifier and command listener (/kill, /status)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

import httpx

from src.core.config import BotConfig

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends real-time alerts to Telegram and processes incoming operator commands
    from the configured authorized chat_id.
    """

    def __init__(
        self,
        config: BotConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._token = config.secrets.telegram_bot_token
        self._chat_id = config.secrets.telegram_chat_id
        self._enabled = config.telegram.enabled and bool(self._token and self._chat_id)
        self._notify_on = set(config.telegram.notify_on)

        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=10.0)
        self._last_update_id: int = 0

        # Sent message log (for testing/inspection when disabled or mocked)
        self.sent_messages: list[tuple[str, str]] = []

    def reload_config(self, config: BotConfig) -> None:
        """Dynamically reload telegram credentials and activation flag without restart."""
        self.config = config
        self._token = config.secrets.telegram_bot_token
        self._chat_id = config.secrets.telegram_chat_id
        self._enabled = config.telegram.enabled and bool(self._token and self._chat_id)
        self._notify_on = set(config.telegram.notify_on)
        logger.info("TelegramNotifier reloaded: enabled=%s, chat_id=%s", self._enabled, self._chat_id)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def should_notify(self, event_category: str) -> bool:
        """Check whether notifications are enabled for the given category."""
        return event_category in self._notify_on

    async def send_direct_reply(self, text: str) -> bool:
        """Send a direct response to the operator regardless of category filter."""
        if not self._token or not self._chat_id:
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram command reply sent successfully to %s", self._chat_id)
                return True
            logger.warning("Telegram send_direct_reply failed: HTTP %d %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.error("Telegram send_direct_reply error: %s", e)
            return False
        if not self._external_client:
            await self._client.aclose()

    async def send_message(self, event_category: str, text: str) -> bool:
        """
        Send a notification message if the category is in `notify_on`.
        Always records to `self.sent_messages` for in-memory auditability.
        """
        if not self.should_notify(event_category):
            return False

        self.sent_messages.append((event_category, text))

        if not self._enabled:
            logger.debug("Telegram disabled; queued [%s] message locally", event_category)
            return True

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram notification sent successfully to %s", self._chat_id)
                return True
            logger.warning("Telegram sendMessage returned HTTP %d: %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            logger.error("Failed to send Telegram notification: %s", e)
            return False

    async def poll_commands(
        self,
        on_kill_command: Callable[[str], Coroutine[Any, Any, Any]] | None = None,
        on_status_command: Callable[[], Coroutine[Any, Any, str]] | None = None,
        mock_updates: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """
        Check incoming Telegram updates from the authorized `chat_id` and execute
        `/kill` or `/status` commands.
        Returns list of processed command strings.
        """
        processed: list[str] = []
        updates = mock_updates

        if updates is None:
            if not self._enabled:
                return processed
            url = f"https://api.telegram.org/bot{self._token}/getUpdates"
            params: dict[str, Any] = {"timeout": 1}
            if self._last_update_id > 0:
                params["offset"] = self._last_update_id + 1
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    updates = data.get("result") or []
                else:
                    return processed
            except Exception as e:
                logger.debug("Telegram getUpdates failed: %s", type(e).__name__)
                return processed

        for upd in updates:
            upd_id = int(upd.get("update_id", 0))
            if upd_id > self._last_update_id:
                self._last_update_id = upd_id

            msg = upd.get("message") or {}
            chat_id = str(msg.get("chat", {}).get("id", ""))
            # Security: ignore commands from unauthorized chat IDs if chat_id is configured
            if self._chat_id and chat_id != str(self._chat_id):
                logger.warning("Ignored Telegram command from unauthorized chat_id=%s", chat_id)
                continue

            text = (msg.get("text") or "").strip()
            logger.info("Received Telegram command: '%s' from chat_id=%s", text, chat_id)
            if text.startswith("/start") or text.startswith("/help"):
                processed.append(text.split()[0])
                welcome = (
                    "👋 <b>Solana Meme Trading Bot Aktif</b>\n\n"
                    "Perintah yang tersedia:\n"
                    "• <code>/status</code> - Cek Live Equity, PnL, & Posisi Terbuka\n"
                    "• <code>/kill</code> - Emergency Kill Switch (Tutup semua posisi & stop bot)\n"
                )
                await self.send_direct_reply(welcome)
            elif text.startswith("/kill"):
                reason = text[len("/kill"):].strip() or "Operator sent /kill via Telegram"
                processed.append("/kill")
                if on_kill_command is not None:
                    await on_kill_command(reason)
                await self.send_direct_reply(f"🛑 Kill switch activated: {reason}")
            elif text.startswith("/status"):
                processed.append("/status")
                if on_status_command is not None:
                    status_text = await on_status_command()
                    await self.send_direct_reply(status_text)

        return processed

